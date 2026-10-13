"""
Modelo tecnoeconómico de dimensionamiento FV + batería para un hotel.

Barre combinaciones n_paneles × n_baterias, simula hora a hora durante todo el
horizonte económico (con degradación FV y de batería) para cada año
meteorológico disponible, y calcula VAN, TIR, ROI y CAPEX de cada configuración.

Entrega la matriz completa y selecciona como óptima la configuración de mayor
VAN esperado (media de los años meteorológicos). El VAN mínimo entre años
meteorológicos se reporta como medida del riesgo climático, y la TIR y el ROI
(= (Σ flujos − CAPEX) / CAPEX, sin descontar) como indicadores complementarios.

Inversor:
Se instalan tantos inversores como exige la potencia FV (límite de potencia DC
por inversor). La potencia de carga/descarga de la batería se limita a la del
conjunto de inversores, además de por su C-rate.

PVGIS:
La serie FV se utiliza directamente como producción útil neta del sistema,
incorporando las pérdidas configuradas en PVGIS. No se reconstruye una
producción DC dividiendo por la eficiencia del inversor ni se vuelve a aplicar
una pérdida de inversor a la FV.

EMS:
FV -> consumo -> batería -> red. La batería sólo carga con excedente FV y no
se implementa una estrategia específica de peak shaving. Los excedentes que
no caben en la batería se vierten sin remuneración.

Uso: python modelo_fv_bateria.py
Requiere: numpy, pandas, matplotlib
"""

# =============================================================================
# INPUTS  (todo lo modificable está aquí; el resto del fichero sólo calcula)
# =============================================================================

ETIQUETA_EJECUCION = None   # por defecto, el nombre de BATERIA
CARPETA_RESULTADOS = "resultados"

RUTA_DEMANDA = "datos/demanda_hotel.csv"
COL_DEMANDA = "demanda_kWh"

RUTA_FV = "datos/fv_especifica.csv"
COLS_FV = None
# PVGIS entrega las series horarias en UTC; Puerto Escondido está en UTC-6
# (sin horario de verano desde 2022). Si el CSV ya está en hora local, 0.
# Si está en UTC tal cual sale de PVGIS, -6. El modelo comprueba al cargar
# que el mediodía solar caiga entre las 11 y las 14 h y si no, se detiene.
DESFASE_HORARIO_FV_H = -6   # Anexo 1 en UTC -> hora local de Oaxaca (UTC-6)

RUTA_TARIFA_HORARIA = "datos/tarifa_energia.csv"
COL_MES = "mes"
COL_TARIFA_ENERGIA = "tarifa_energia_MXN_kWh"

RUTA_TARIFA_MENSUAL = "datos/tarifa_mensual.csv"
COL_TARIFA_DEMANDA = "tarifa_demanda_MXN_kW"
COL_CARGO_FIJO = "cargo_fijo_MXN_mes"

CSV_SEP = ","
CSV_DECIMAL = "."

# --- FV ----------------------------------------------------------------------
# Módulo JA Solar JAM72D42-645/LB (JA Solar, 2024)
POTENCIA_PANEL_KWP = 0.645
# Coste total instalado por panel, sin IVA: módulo (2.984,81) + instalación y
# componentes auxiliares (5.418) (Tienda Solar, s.f.; PSE, 2026)
PRECIO_PANEL_MXN = 8_403.0
DEGRADACION_FV_ANUAL = 0.0045
VIDA_UTIL_FV_ANOS = 30

# --- Batería -----------------------------------------------------------------
# Escenario a simular: "BYD" (primera vida) o "LEAF" (segunda vida).
BATERIA = "BYD"

BATERIAS = {
    # BYD Battery-Box Premium HVS 5.1 (BYD, s.f.-a; s.f.-b; AutoSolar, s.f.)
    "BYD": dict(
        CAPACIDAD_BATERIA_KWH=5.12,
        PRECIO_BATERIA_MXN=47_393.0,
        SOH_INICIAL=1.00,
        SOH_FIN_VIDA=0.60,
        SOC_MIN=0.00,
        SOC_MAX=1.00,
        EFICIENCIA_ROUND_TRIP=0.96,
        C_RATE_MAX=1.0,
        DEGRADACION_BAT_POR_CICLO=0.00013,
        # Envejecimiento por calendario (pérdida de SOH por año, independiente
        # del uso). PENDIENTE de fuente; 0 = no se modela.
        DEGRADACION_BAT_CALENDARIO_ANUAL=0.0,
        # Coste adicional por unidad no incluido en PRECIO_BATERIA_MXN.
        # El HVS 5.1 ya incluye BCU + base, así que 0.
        COSTE_ADICIONAL_BAT_MXN=0.0,
        # 0–85 unidades, paso 5 (capacidad útil máx. ≈ consumo medio diario)
        RANGO_BATERIAS=list(range(0, 86, 5)),
    ),
    # Pack Nissan Leaf Gen1 24 kWh de segunda vida (Gao et al., 2024)
    "LEAF": dict(
        CAPACIDAD_BATERIA_KWH=24.0,
        PRECIO_BATERIA_MXN=21_070.0,
        SOH_INICIAL=0.66,
        SOH_FIN_VIDA=0.50,
        SOC_MIN=0.10,
        SOC_MAX=0.90,
        EFICIENCIA_ROUND_TRIP=0.976,
        C_RATE_MAX=0.2,
        DEGRADACION_BAT_POR_CICLO=0.00010,
        # Envejecimiento por calendario. PENDIENTE de fuente; 0 = no se modela.
        DEGRADACION_BAT_CALENDARIO_ANUAL=0.0,
        # BMS compatible con el inversor, integración, envolvente, protecciones.
        # PENDIENTE de fuente: comprobar qué incluye el precio de Gao et al.
        COSTE_ADICIONAL_BAT_MXN=0.0,
        # 0–34 packs, paso 2 (capacidad útil máx. ≈ consumo medio diario)
        RANGO_BATERIAS=list(range(0, 35, 2)),
    ),
}
globals().update(BATERIAS[BATERIA])
SOC_INICIAL = SOC_MIN

# Base para contar ciclos equivalentes (y por tanto degradación):
#   "nominal": energía descargada / capacidad nominal (E_nom)
#   "util":    energía descargada / capacidad útil inicial
#              (E_nom · SOH_INICIAL · (SOC_MAX − SOC_MIN))
# Debe coincidir con cómo define la fuente DEGRADACION_BAT_POR_CICLO.
BASE_CICLOS = "nominal"

# --- Inversor ----------------------------------------------------------------
# Inversor híbrido de 30 kW (cubre el pico de demanda de 28,92 kW).
# Referencia: Deye SUN-30K-SG01HP3-EU-BM3, 2.589,73 EUR × 20,395 MXN/EUR
# (sun.store, s.f.; BCE, 2026).
PRECIO_INVERSOR_MXN = 52_817.0
# Potencia FV máxima conectable por inversor (máx. potencia DC: 39 kW).
P_FV_MAX_POR_INVERSOR_KW = 39.0
# Potencia máxima de carga/descarga de batería por inversor (se toma la
# potencia nominal del inversor, 30 kW).
P_BAT_MAX_POR_INVERSOR_KW = 30.0
# Nº de inversores = ceil(P_fv / P_FV_MAX_POR_INVERSOR_KW). Las salidas AC
# en paralelo nunca superan la demanda porque no se inyecta a red.

# La producción PVGIS se usa directamente como producción útil neta.
# No se aplica una segunda eficiencia del inversor a la FV.

# --- OPEX --------------------------------------------------------------------
# OPEX FV fijo: inspección técnica anual (Habitissimo, 2026), sin IVA.
# La limpieza la realiza el personal del alojamiento (sin coste adicional).
OPEX_FV_FIJO_MXN_ANO = 5_517.0
OPEX_BAT_MXN_KWH_ANO = 0.0   # despreciable para ambas baterías
INFLACION_OPEX = 0.03

# --- Economía ----------------------------------------------------------------
INFLACION_TARIFA = 0.03   # solo se usa si la tarifa trae un único año
TASA_DESCUENTO = 0.03   # nominal; los flujos están en términos nominales
HORIZONTE_ANOS = 10

# --- Ambiental ----------------------------------------------------------------
# Factor de emisión del Sistema Eléctrico Nacional 2024 (SEMARNAT, 2025)
FACTOR_EMISION_TCO2_MWH = 0.444

# --- Valor residual ----------------------------------------------------------
CRITERIO_VR_FV = "lineal"
CRITERIO_VR_BAT = "soh"

# --- Barrido -----------------------------------------------------------------
# Límite de espacio: A_disp = 1.000 m², módulo 2,80 m², k = 1,27
# (separación entre filas IDAE con inclinación 19°)
# -> N_p,max = 1.000 / (1,27 · 2,80) ≈ 282 -> 280 paneles (180,6 kWp)
RANGO_PANELES = list(range(10, 281, 10))
# RANGO_BATERIAS se define en el diccionario de cada batería

if ETIQUETA_EJECUCION is None:
    ETIQUETA_EJECUCION = BATERIA

# =============================================================================
# FIN DE INPUTS
# =============================================================================

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
import numpy as np
import pandas as pd


def leer_csv(ruta):
    return pd.read_csv(ruta, sep=CSV_SEP, decimal=CSV_DECIMAL)


def cargar_datos():
    demanda = leer_csv(RUTA_DEMANDA)[COL_DEMANDA].to_numpy(float)
    L = len(demanda)

    df_fv = leer_csv(RUTA_FV)
    cols = COLS_FV if COLS_FV is not None else list(df_fv.columns)
    fv = {
        str(c): np.roll(df_fv[c].to_numpy(float), DESFASE_HORARIO_FV_H)
        for c in cols
    }
    for c, serie in fv.items():
        comprobar_fv(c, serie, L)

    return demanda, fv, cargar_tarifa(L)


def comprobar_fv(nombre, serie, L):
    """Validaciones de la serie FV específica (kWh/kWp por hora, hora local)."""
    if len(serie) != L:
        raise ValueError(
            f"FV '{nombre}' tiene {len(serie)} filas; demanda tiene {L} "
            "(¿año bisiesto? quitar el 29 de febrero o usar 8.784 h en todo)"
        )
    if np.isnan(serie).any() or serie.min() < 0:
        raise ValueError(f"FV '{nombre}': valores vacíos o negativos")

    anual = serie.sum()
    if not 800 <= anual <= 2_500:
        raise ValueError(
            f"FV '{nombre}': {anual:,.0f} por kWp y año. Se esperan "
            "kWh/kWp (≈1.600). Si viene de PVGIS en W, dividir P entre 1.000."
        )

    perfil = serie[: (L // 24) * 24].reshape(-1, 24).mean(0)
    centro = (perfil * (np.arange(24) + 0.5)).sum() / perfil.sum()
    if not 11 <= centro <= 14:
        raise ValueError(
            f"FV '{nombre}': el centro de la producción diaria cae a las "
            f"{centro:.1f} h. La serie no está en hora local; revisar "
            "DESFASE_HORARIO_FV_H (PVGIS en UTC -> -6 para Oaxaca)."
        )


def por_anos(serie, filas_ano, nombre):
    if len(serie) == filas_ano:
        return serie.reshape(1, filas_ano), False
    if len(serie) == HORIZONTE_ANOS * filas_ano:
        return serie.reshape(HORIZONTE_ANOS, filas_ano), True
    raise ValueError(
        f"{nombre}: {len(serie)} filas; debe tener {filas_ano} "
        f"o {HORIZONTE_ANOS * filas_ano}"
    )


def cargar_tarifa(L):
    dh = leer_csv(RUTA_TARIFA_HORARIA)
    energia, energia_evol = por_anos(
        dh[COL_TARIFA_ENERGIA].to_numpy(float), L, "Tarifa de energía"
    )
    mes_h = dh[COL_MES].to_numpy(int)[:L]

    dm = leer_csv(RUTA_TARIFA_MENSUAL)
    meses = dm[COL_MES].to_numpy(int)
    if len(meses) % 12 or not (meses.reshape(-1, 12) == np.arange(1, 13)).all():
        raise ValueError("Fichero mensual: cada año debe tener los meses 1..12 en orden")
    if mes_h.min() < 1 or mes_h.max() > 12:
        raise ValueError("Fichero horario: la columna de mes debe valer 1..12")

    demanda, demanda_evol = por_anos(
        dm[COL_TARIFA_DEMANDA].to_numpy(float), 12, "Tarifa de demanda"
    )
    fijo, fijo_evol = por_anos(
        dm[COL_CARGO_FIJO].to_numpy(float), 12, "Cargo fijo"
    )

    return dict(
        mes_h=mes_h,
        energia=(energia, energia_evol),
        demanda=(demanda, demanda_evol),
        fijo=(fijo, fijo_evol),
    )


def tarifa_del_ano(componente, y):
    valores, ya_evolucionada = componente
    if ya_evolucionada:
        return valores[y]
    return valores[0] * (1 + INFLACION_TARIFA) ** y


def factura_anual(E_red, mes_h, t_energia, t_demanda, cargo_fijo):
    n = E_red.shape[1]
    energia, demanda, fijo = np.zeros(n), np.zeros(n), np.zeros(n)

    for m in np.unique(mes_h):
        h = mes_h == m
        energia += t_energia[h] @ E_red[h]
        demanda += E_red[h].max(0) * t_demanda[m - 1]
        fijo += cargo_fijo[m - 1]

    return energia, demanda, fijo


def comprobar_balance(
    demanda, fv, directo, carga, descarga, red, vertido,
    soc_inicio, soc_fin, eta_c, eta_d, y, tol=1e-6
):
    """Comprueba conservación de energía y balance interno de la batería."""
    escala = max(1.0, float(demanda.max()), float(fv.max()))

    err_demanda = np.abs(
        demanda[:, None] - directo - descarga - red
    ).max()

    err_fv = np.abs(
        fv - directo - carga - vertido
    ).max()

    err_bat = np.abs(
        (soc_fin - soc_inicio)
        - (carga.sum(0) * eta_c - descarga.sum(0) / eta_d)
    ).max()

    minimo = min(red.min(), vertido.min(), carga.min(), descarga.min())

    if (
        err_demanda > tol * escala
        or err_fv > tol * escala
        or err_bat > tol * escala * len(demanda)
        or minimo < -tol * escala
    ):
        raise AssertionError(
            f"Balance energético no cierra (año {y + 1}): "
            f"demanda {err_demanda:.2e}, FV {err_fv:.2e}, "
            f"batería {err_bat:.2e} kWh, flujo mínimo {minimo:.2e} kWh"
        )


def n_inversores(P_fv):
    return np.where(
        P_fv > 0, np.ceil(P_fv / P_FV_MAX_POR_INVERSOR_KW - 1e-9), 1.0
    )


def capacidad_ciclo(E_nom):
    if BASE_CICLOS == "nominal":
        return E_nom
    if BASE_CICLOS == "util":
        return E_nom * SOH_INICIAL * (SOC_MAX - SOC_MIN)
    raise ValueError(f"BASE_CICLOS desconocido: {BASE_CICLOS}")


def simular_escenario(fv_kwh_kwp, demanda, tarifa, P_fv, E_nom):
    """
    El perfil FV de PVGIS se considera producción útil neta del sistema.

    EMS:
      FV -> consumo -> batería -> red

    La batería carga únicamente con excedente FV.
    No hay carga desde red ni optimización específica de peak shaving.
    """
    H, n, L = HORIZONTE_ANOS, len(P_fv), len(demanda)

    eta_c = eta_d = np.sqrt(EFICIENCIA_ROUND_TRIP)

    soh = np.full(n, SOH_INICIAL)
    activa = E_nom > 0
    soc = SOC_INICIAL * E_nom * soh

    nombres = [
        "E_fv", "E_directo", "E_carga", "E_descarga", "E_red", "E_vertido",
        "energia_con", "demanda_con", "fijo_con", "soh_fin", "bat_activa",
        "efc", "bat_activa_fin"
    ]
    r = {k: np.zeros((H, n)) for k in nombres}
    for k in ("energia_sin", "demanda_sin", "fijo_sin"):
        r[k] = np.zeros(H)

    carga = np.zeros((L, n))
    descarga = np.zeros((L, n))

    for y in range(H):
        # 1) Producción FV útil neta del año
        fv = (
            np.outer(fv_kwh_kwp, P_fv)
            * (1 - DEGRADACION_FV_ANUAL) ** y
        )

        # 2) Cobertura directa de la demanda
        directo = np.minimum(fv, demanda[:, None])
        excedente = fv - directo
        deficit = demanda[:, None] - directo

        # 3) Límites de batería
        cap = E_nom * soh * activa
        e_min, e_max = SOC_MIN * cap, SOC_MAX * cap
        pm = np.minimum(
            C_RATE_MAX * cap,
            n_inversores(P_fv) * P_BAT_MAX_POR_INVERSOR_KW,
        )
        soc = np.clip(soc, e_min, e_max)

        soc_inicio = soc.copy()

        # 4) EMS horario
        for h in range(L):
            c = np.minimum(
                excedente[h],
                np.minimum(pm, np.maximum(e_max - soc, 0) / eta_c)
            )
            soc = soc + c * eta_c

            d = np.minimum(
                deficit[h],
                np.minimum(pm, np.maximum(soc - e_min, 0) * eta_d)
            )
            soc = soc - d / eta_d

            carga[h] = c
            descarga[h] = d

        red = deficit - descarga
        vertido = excedente - carga

        comprobar_balance(
            demanda, fv, directo, carga, descarga, red, vertido,
            soc_inicio, soc, eta_c, eta_d, y
        )

        # 5) Facturación
        t_e = tarifa_del_ano(tarifa["energia"], y)
        t_d = tarifa_del_ano(tarifa["demanda"], y)
        t_f = tarifa_del_ano(tarifa["fijo"], y)

        (
            r["energia_sin"][y],
            r["demanda_sin"][y],
            r["fijo_sin"][y],
        ) = (
            v[0]
            for v in factura_anual(
                demanda[:, None], tarifa["mes_h"], t_e, t_d, t_f
            )
        )

        (
            r["energia_con"][y],
            r["demanda_con"][y],
            r["fijo_con"][y],
        ) = factura_anual(
            red, tarifa["mes_h"], t_e, t_d, t_f
        )

        # 6) Balances anuales
        r["E_fv"][y] = fv.sum(0)
        r["E_directo"][y] = directo.sum(0)
        r["E_carga"][y] = carga.sum(0)
        r["E_descarga"][y] = descarga.sum(0)
        r["E_red"][y] = red.sum(0)
        r["E_vertido"][y] = vertido.sum(0)
        r["bat_activa"][y] = activa

        # 7) Degradación de batería al cierre del año
        e_ciclo = capacidad_ciclo(E_nom)
        efc = np.divide(
            descarga.sum(0) / eta_d,
            e_ciclo,
            out=np.zeros(n),
            where=e_ciclo > 0,
        )
        r["efc"][y] = efc

        soh = np.where(
            activa,
            soh
            - DEGRADACION_BAT_POR_CICLO * efc
            - DEGRADACION_BAT_CALENDARIO_ANUAL,
            soh,
        )

        activa = activa & (soh > SOH_FIN_VIDA)
        r["soh_fin"][y] = soh
        r["bat_activa_fin"][y] = activa

    return r


def capex(n_pan, n_bat):
    inversor = np.where(
        n_pan + n_bat > 0,
        n_inversores(n_pan * POTENCIA_PANEL_KWP) * PRECIO_INVERSOR_MXN,
        0.0,
    )
    return (
        n_pan * PRECIO_PANEL_MXN
        + n_bat * (PRECIO_BATERIA_MXN + COSTE_ADICIONAL_BAT_MXN)
        + inversor
    )


def valor_residual(n_pan, n_bat, soh_final):
    if CRITERIO_VR_FV == "lineal":
        vr_fv = (
            n_pan
            * PRECIO_PANEL_MXN
            * max(0.0, 1 - HORIZONTE_ANOS / VIDA_UTIL_FV_ANOS)
        )
    elif CRITERIO_VR_FV == "ninguno":
        vr_fv = np.zeros(len(n_pan))
    else:
        raise ValueError(f"CRITERIO_VR_FV desconocido: {CRITERIO_VR_FV}")

    if CRITERIO_VR_BAT == "soh":
        frac = np.clip(
            (soh_final - SOH_FIN_VIDA)
            / (SOH_INICIAL - SOH_FIN_VIDA),
            0,
            1,
        )
        vr_bat = n_bat * (PRECIO_BATERIA_MXN + COSTE_ADICIONAL_BAT_MXN) * frac
    elif CRITERIO_VR_BAT == "ninguno":
        vr_bat = np.zeros(len(n_bat))
    else:
        raise ValueError(f"CRITERIO_VR_BAT desconocido: {CRITERIO_VR_BAT}")

    return vr_fv, vr_bat


def flujos_anuales(r, n_pan, n_bat, P_fv, E_nom):
    H = HORIZONTE_ANOS
    f_opex = (1 + INFLACION_OPEX) ** np.arange(H)[:, None]

    sin = {
        k: np.broadcast_to(r[k + "_sin"][:, None], r[k + "_con"].shape)
        for k in ("energia", "demanda", "fijo")
    }

    factura_sin = sin["energia"] + sin["demanda"] + sin["fijo"]
    factura_con = r["energia_con"] + r["demanda_con"] + r["fijo_con"]
    ahorro = factura_sin - factura_con

    opex_fv = OPEX_FV_FIJO_MXN_ANO * (n_pan > 0)[None, :] * f_opex
    opex_bat = (
        OPEX_BAT_MXN_KWH_ANO
        * E_nom[None, :]
        * r["bat_activa"]
        * f_opex
    )

    vr_fv, vr_bat = valor_residual(
        n_pan, n_bat, r["soh_fin"][-1]
    )

    F = np.zeros((H + 1, len(n_pan)))
    F[0] = -capex(n_pan, n_bat)
    F[1:] = ahorro - opex_fv - opex_bat
    F[H] += vr_fv + vr_bat

    detalle = dict(
        coste_energia_sin=sin["energia"],
        coste_demanda_sin=sin["demanda"],
        cargo_fijo_sin=sin["fijo"],
        factura_sin=factura_sin,
        coste_energia_con=r["energia_con"],
        coste_demanda_con=r["demanda_con"],
        cargo_fijo_con=r["fijo_con"],
        factura_con=factura_con,
        ahorro_energia=sin["energia"] - r["energia_con"],
        ahorro_demanda=sin["demanda"] - r["demanda_con"],
        ahorro_total=ahorro,
        opex_fv=opex_fv,
        opex_bat=opex_bat,
    )

    return F, detalle, vr_fv, vr_bat


def van(F, tasa):
    t = np.arange(F.shape[0])[:, None]
    return (F / (1 + tasa) ** t).sum(0)


def tir(F, lo=-0.99, hi=10.0, iters=200):
    n = F.shape[1]
    a, b = np.full(n, lo), np.full(n, hi)

    fa = van(F, a)
    valida = (
        (np.sign(fa) != np.sign(van(F, b)))
        & (F[0] < 0)
    )

    for _ in range(iters):
        m = (a + b) / 2
        fm = van(F, m)

        izq = np.sign(fm) == np.sign(fa)
        a = np.where(izq, m, a)
        fa = np.where(izq, fm, fa)
        b = np.where(izq, b, m)

    return np.where(valida, (a + b) / 2, np.nan)


def roi(F):
    # ROI = (Σ B_t − I_0) / I_0 · 100, sin descontar (definición de la memoria).
    # B_t: flujo neto anual (ahorro − OPEX, + valor residual en el año N).
    cap = -F[0]
    return (
        np.divide(
            F[1:].sum(0) - cap,
            cap,
            out=np.full(F.shape[1], np.nan),
            where=cap > 0,
        )
        * 100
    )


def indicadores(F):
    return dict(
        VAN_MXN=van(F, TASA_DESCUENTO),
        TIR_pct=tir(F) * 100,
        ROI_pct=roi(F),
    )


def tabla_resumen(
    n_pan, n_bat, P_fv, E_nom, r, F, vr_fv, vr_bat, demanda_anual
):
    al_hotel = r["E_directo"] + r["E_descarga"]

    with np.errstate(divide="ignore", invalid="ignore"):
        # Años completos de operación si la batería llega a fin de vida dentro
        # del horizonte (incluido el último año); NaN si sigue activa al final.
        anos_operativos = np.where(
            (r["bat_activa_fin"] == 0).any(0) & (E_nom > 0),
            (r["bat_activa"] == 1).sum(0),
            np.nan,
        )

        frac_vertida = np.divide(
            r["E_vertido"][0],
            r["E_fv"][0],
            out=np.full(len(n_pan), np.nan),
            where=r["E_fv"][0] > 0,
        )

        df = pd.DataFrame({
            "n_paneles": n_pan,
            "n_baterias": n_bat,
            "P_fv_kWp": P_fv,
            "E_bat_nom_kWh": E_nom,
            "n_inversores": n_inversores(P_fv),
            "CAPEX_MXN": -F[0],
            **indicadores(F),

            "ahorro_energia_ano1_MXN":
                r["energia_sin"][0] - r["energia_con"][0],

            "ahorro_demanda_ano1_MXN":
                r["demanda_sin"][0] - r["demanda_con"][0],

            "ahorro_total_ano1_MXN":
                (
                    r["energia_sin"][0]
                    + r["demanda_sin"][0]
                    + r["fijo_sin"][0]
                )
                - (
                    r["energia_con"][0]
                    + r["demanda_con"][0]
                    + r["fijo_con"][0]
                ),

            "flujo_ano1_MXN": F[1],

            "VR_fv_MXN": vr_fv,
            "VR_bat_MXN": vr_bat,

            "E_fv_ano1_kWh": r["E_fv"][0],
            "E_fv_directa_ano1_kWh": r["E_directo"][0],
            "E_descarga_bat_ano1_kWh": r["E_descarga"][0],
            "E_red_ano1_kWh": r["E_red"][0],
            "E_vertido_ano1_kWh": r["E_vertido"][0],

            "aprovechamiento_FV_ano1_pct":
                (1 - frac_vertida) * 100,

            "autosuficiencia_ano1_pct":
                al_hotel[0] / demanda_anual * 100,

            "CO2_evitado_ano1_t":
                (demanda_anual - r["E_red"][0])
                * FACTOR_EMISION_TCO2_MWH / 1000,

            "CO2_evitado_total_t":
                (demanda_anual * HORIZONTE_ANOS - r["E_red"].sum(0))
                * FACTOR_EMISION_TCO2_MWH / 1000,

            "E_fv_total_kWh": r["E_fv"].sum(0),
            "E_red_total_kWh": r["E_red"].sum(0),

            "ciclos_equiv_totales": r["efc"].sum(0),

            "SOH_final":
                np.where(
                    E_nom > 0,
                    r["soh_fin"][-1],
                    np.nan,
                ),

            "anos_operativos_bat":
                anos_operativos,
        })

    return df


def heatmap(df, col, titulo, unidad, centro, ruta):
    piv = df.pivot(
        index="n_baterias",
        columns="n_paneles",
        values=col,
    )

    escala = 1e6 if col == "VAN_MXN" else 1.0
    z = piv.to_numpy(float) / escala
    centro = centro / escala

    fin = z[np.isfinite(z)]

    fig, ax = plt.subplots(
        figsize=(
            1.0 + 0.75 * z.shape[1],
            1.5 + 0.5 * z.shape[0],
        )
    )

    cmap = plt.get_cmap("RdBu").copy()
    cmap.set_bad("#d9d9d9")

    if fin.size and fin.min() < centro < fin.max():
        norm = TwoSlopeNorm(
            vcenter=centro,
            vmin=fin.min(),
            vmax=fin.max(),
        )
    else:
        norm = None

    im = ax.imshow(
        np.ma.masked_invalid(z),
        origin="lower",
        aspect="auto",
        cmap=cmap,
        norm=norm,
    )

    ax.set_xticks(
        range(z.shape[1]),
        [int(v) for v in piv.columns],
    )

    ax.set_yticks(
        range(z.shape[0]),
        [int(v) for v in piv.index],
    )

    ax.set_xlabel("Nº de paneles")
    ax.set_ylabel("Nº de baterías")
    ax.set_title(
        f"{titulo} — {ETIQUETA_EJECUCION}",
        loc="left",
    )

    for s in ax.spines.values():
        s.set_visible(False)

    if z.size <= 400:
        fmt = "{:.2f}" if col == "VAN_MXN" else "{:.1f}"

        for i in range(z.shape[0]):
            for j in range(z.shape[1]):
                if np.isfinite(z[i, j]):
                    oscuro = abs(im.norm(z[i, j]) - 0.5) > 0.3
                    ax.text(
                        j,
                        i,
                        fmt.format(z[i, j]),
                        ha="center",
                        va="center",
                        fontsize=7,
                        color="white" if oscuro else "#1a1a1a",
                    )

    cb = fig.colorbar(im, ax=ax)
    cb.set_label(unidad)
    cb.outline.set_visible(False)

    fig.tight_layout()
    fig.savefig(ruta, dpi=150)
    plt.close(fig)


def tabla_anual(n_pan, n_bat, F, detalle, vr):
    H = HORIZONTE_ANOS
    filas = []

    for y in range(H + 1):
        bloque = pd.DataFrame({
            "n_paneles": n_pan,
            "n_baterias": n_bat,
            "ano": y,
        })

        for k in detalle:
            bloque[k + "_MXN"] = (
                detalle[k][y - 1]
                if y > 0
                else 0.0
            )

        bloque["capex_MXN"] = -F[0] if y == 0 else 0.0
        bloque["valor_residual_MXN"] = vr if y == H else 0.0
        bloque["flujo_neto_MXN"] = F[y]
        bloque["flujo_descontado_MXN"] = (
            F[y] / (1 + TASA_DESCUENTO) ** y
        )

        filas.append(bloque)

    return pd.concat(filas).sort_values(
        ["n_paneles", "n_baterias", "ano"]
    )


def main():
    out = Path(CARPETA_RESULTADOS) / ETIQUETA_EJECUCION
    out.mkdir(parents=True, exist_ok=True)

    demanda, fv_escenarios, tarifa = cargar_datos()

    malla = np.array(
        [
            (p, b)
            for p in RANGO_PANELES
            for b in RANGO_BATERIAS
        ],
        dtype=float,
    )

    n_pan, n_bat = malla[:, 0], malla[:, 1]
    P_fv = n_pan * POTENCIA_PANEL_KWP
    E_nom = n_bat * CAPACIDAD_BATERIA_KWH

    tablas = []
    flujos = []
    detalles = []
    vrs = []
    anuales = []

    for nombre, fv in fv_escenarios.items():
        print(
            f"Simulando escenario meteorológico '{nombre}' "
            f"({len(malla)} configuraciones)..."
        )

        r = simular_escenario(
            fv, demanda, tarifa, P_fv, E_nom
        )

        F, det, vr_fv, vr_bat = flujos_anuales(
            r, n_pan, n_bat, P_fv, E_nom
        )

        t = tabla_resumen(
            n_pan, n_bat, P_fv, E_nom,
            r, F, vr_fv, vr_bat, demanda.sum()
        )

        t.insert(0, "escenario", nombre)
        tablas.append(t)
        flujos.append(F)
        detalles.append(det)
        vrs.append(vr_fv + vr_bat)

        a = tabla_anual(
            n_pan, n_bat, F, det, vr_fv + vr_bat
        )

        a.insert(0, "escenario", nombre)
        anuales.append(a)

    pd.concat(tablas).to_csv(
        out / "resultados_por_escenario.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.concat(anuales).to_csv(
        out / "flujos_anuales_por_escenario.csv",
        index=False,
        encoding="utf-8-sig",
    )

    F_med = np.mean(flujos, axis=0)

    cols_num = [
        c
        for c in tablas[0].columns
        if c not in ("escenario", "n_paneles", "n_baterias")
    ]

    medio = (
        pd.concat(tablas)
        .groupby(
            ["n_paneles", "n_baterias"],
            sort=False,
        )[cols_num]
        .mean()
        .reset_index()
    )

    for k, v in indicadores(F_med).items():
        medio[k] = v

    medio.insert(
        2,
        "n_escenarios",
        len(fv_escenarios),
    )

    # Riesgo climático: VAN del peor año meteorológico
    medio["VAN_min_MXN"] = np.min(
        [van(f, TASA_DESCUENTO) for f in flujos], axis=0
    )

    # Selección del óptimo: máximo VAN esperado
    opt = medio.loc[[medio["VAN_MXN"].idxmax()]]
    opt.to_csv(
        out / "optimo.csv",
        index=False,
        encoding="utf-8-sig",
    )
    o = opt.iloc[0]
    print(
        f"Óptimo ({ETIQUETA_EJECUCION}): {int(o.n_paneles)} paneles "
        f"({o.P_fv_kWp:.1f} kWp), {int(o.n_baterias)} baterías | "
        f"VAN esperado {o.VAN_MXN:,.0f} MXN, VAN mínimo {o.VAN_min_MXN:,.0f} MXN, "
        f"TIR {o.TIR_pct:.1f} %, ROI {o.ROI_pct:.1f} %"
    )
    if o.n_paneles == max(RANGO_PANELES) or (
        o.n_baterias == max(RANGO_BATERIAS) and o.n_baterias > 0
    ):
        print("  AVISO: el óptimo está en el límite de la rejilla.")

    medio.to_csv(
        out / "resultados_medios.csv",
        index=False,
        encoding="utf-8-sig",
    )

    det_med = {
        k: np.mean(
            [d[k] for d in detalles],
            axis=0,
        )
        for k in detalles[0]
    }

    tabla_anual(
        n_pan,
        n_bat,
        F_med,
        det_med,
        np.mean(vrs, axis=0),
    ).to_csv(
        out / "flujos_anuales_medios.csv",
        index=False,
        encoding="utf-8-sig",
    )

    heatmap(
        medio,
        "VAN_MXN",
        "VAN",
        "VAN [millones MXN]",
        0.0,
        out / "heatmap_VAN.png",
    )

    heatmap(
        medio,
        "TIR_pct",
        f"TIR (centro = tasa de descuento {TASA_DESCUENTO:.0%})",
        "TIR [%]",
        TASA_DESCUENTO * 100,
        out / "heatmap_TIR.png",
    )

    heatmap(
        medio,
        "ROI_pct",
        "ROI",
        "ROI [%]",
        0.0,
        out / "heatmap_ROI.png",
    )

    print(f"Resultados guardados en: {out.resolve()}")


if __name__ == "__main__":
    main()
