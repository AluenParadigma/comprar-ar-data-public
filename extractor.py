import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURACION
# ============================================================

SOURCE_URL = (
    "https://comprar.gob.ar/"
    "Compras.aspx?qs=W1HXHGHtH10="
)

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

TIMEOUT_MS = 120000

# Espera breve entre paginas de detalle.
DETAIL_WAIT_MS = 300

# Limitamos el texto del detalle para que latest.json
# no crezca indefinidamente.
MAX_DETAIL_TEXT = 8000


# ============================================================
# UTILIDADES
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    value = str(value)

    value = value.replace("\xa0", " ")

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalize_multiline_text(value):
    if not value:
        return ""

    lines = []

    for line in str(value).splitlines():
        line = clean_text(line)

        if line:
            lines.append(line)

    return "\n".join(lines)


# ============================================================
# TOTAL INFORMADO POR COMPR.AR
# ============================================================

def extract_portal_total(page):
    body_text = page.locator(
        "body"
    ).inner_text()

    patterns = [
        (
            r"Se\s+han\s+encontrado\s*"
            r"\(\s*([\d\.\,]+)\s*\)"
            r"\s*resultados"
        ),
        (
            r"Se\s+han\s+encontrado\s+"
            r"([\d\.\,]+)"
            r"\s+resultados"
        ),
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            body_text,
            flags=re.IGNORECASE,
        )

        if match:

            raw_total = match.group(1)

            total = int(
                raw_total
                .replace(".", "")
                .replace(",", "")
            )

            return total

    raise RuntimeError(
        "No se pudo identificar el total "
        "de resultados informado por COMPR.AR."
    )


# ============================================================
# DETECTAR FILAS DE PROCESOS
# ============================================================

def extract_process_rows(page):
    """
    Busca filas que tengan las 7 columnas que muestra
    la pantalla de Licitaciones de apertura próxima.

    Columnas:
    1. Número de Proceso
    2. Nombre descriptivo
    3. Tipo
    4. Fecha de Apertura
    5. Estado
    6. Unidad Ejecutora
    7. Servicio Administrativo Financiero
    """

    rows = page.locator("tr")

    records = []

    for index in range(rows.count()):

        row = rows.nth(index)

        cells = row.locator("td")

        if cells.count() < 7:
            continue

        values = []

        for cell_index in range(7):

            values.append(
                clean_text(
                    cells
                    .nth(cell_index)
                    .inner_text()
                )
            )

        numero_proceso = values[0]

        if not numero_proceso:
            continue

        if "número de proceso" in numero_proceso.lower():
            continue

        # ----------------------------------------------------
        # LINK AL DETALLE
        # ----------------------------------------------------

        process_url = ""

        first_cell = cells.nth(0)

        anchors = first_cell.locator("a")

        if anchors.count() > 0:

            href = anchors.first.get_attribute(
                "href"
            )

            if href:

                href = href.strip()

                if not href.lower().startswith(
                    "javascript:"
                ):

                    process_url = urljoin(
                        page.url,
                        href,
                    )

        record = {
            "numero_proceso":
                numero_proceso,

            "nombre_proceso":
                values[1],

            "tipo_proceso":
                values[2],

            "fecha_apertura":
                values[3],

            "estado":
                values[4],

            "unidad_ejecutora":
                values[5],

            "servicio_administrativo_financiero":
                values[6],

            "process_url":
                process_url,
        }

        records.append(
            record
        )

    return records


# ============================================================
# ASP.NET PAGINATION
# ============================================================

def find_pager_target(page):
    """
    COMPR.AR utiliza ASP.NET.

    Buscamos el control que hace los postbacks de paginación,
    por ejemplo:

    javascript:__doPostBack(
        'ctl00$...$GridView',
        'Page$2'
    )

    Con ese target podemos solicitar directamente Page$1,
    Page$2, Page$3, etc., aunque el número no esté visible
    en el paginador.
    """

    anchors = page.locator("a")

    for index in range(anchors.count()):

        href = (
            anchors
            .nth(index)
            .get_attribute("href")
            or ""
        )

        match = re.search(
            (
                r"__doPostBack\("
                r"'([^']+)'"
                r","
                r"'Page\$(\d+)'"
                r"\)"
            ),
            href,
            flags=re.IGNORECASE,
        )

        if match:

            return match.group(1)

    return None


def go_to_page_number(
    page,
    pager_target,
    page_number,
):
    """
    Navega directamente a una página del listado
    utilizando el mecanismo ASP.NET.
    """

    if page_number == 1:
        return

    previous_text = ""

    previous_rows = extract_process_rows(
        page
    )

    if previous_rows:

        previous_text = (
            previous_rows[0]
            ["numero_proceso"]
        )

    try:

        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=TIMEOUT_MS,
        ):

            page.evaluate(
                """
                ([target, argument]) => {
                    window.__doPostBack(
                        target,
                        argument
                    );
                }
                """,
                [
                    pager_target,
                    f"Page${page_number}",
                ],
            )

    except PlaywrightTimeoutError:

        # Algunos postbacks pueden resolverse sin
        # navegación completa.
        page.wait_for_timeout(
            1500
        )

    page.wait_for_timeout(
        700
    )

    new_rows = extract_process_rows(
        page
    )

    if not new_rows:

        raise RuntimeError(
            f"La página {page_number} "
            "no devolvió procesos."
        )

    new_first = (
        new_rows[0]
        ["numero_proceso"]
    )

    if (
        previous_text
        and new_first == previous_text
    ):

        raise RuntimeError(
            f"La paginación no avanzó "
            f"a la página {page_number}."
        )


# ============================================================
# EXTRAER TODAS LAS PAGINAS
# ============================================================

def extract_all_processes(page):
    portal_total = extract_portal_total(
        page
    )

    print(
        "Total informado por COMPR.AR:",
        portal_total,
    )

    first_page_records = (
        extract_process_rows(
            page
        )
    )

    if not first_page_records:

        raise RuntimeError(
            "No se encontraron procesos "
            "en la primera página."
        )

    page_size = len(
        first_page_records
    )

    print(
        "Procesos por página:",
        page_size,
    )

    total_pages = math.ceil(
        portal_total / page_size
    )

    print(
        "Páginas esperadas:",
        total_pages,
    )

    pager_target = find_pager_target(
        page
    )

    if (
        total_pages > 1
        and not pager_target
    ):

        raise RuntimeError(
            "No se pudo identificar "
            "el control de paginación ASP.NET."
        )

    all_records = []

    pages_processed = 0

    for page_number in range(
        1,
        total_pages + 1,
    ):

        if page_number > 1:

            print(
                f"Abriendo página "
                f"{page_number}/"
                f"{total_pages}"
            )

            go_to_page_number(
                page,
                pager_target,
                page_number,
            )

        records = (
            extract_process_rows(
                page
            )
        )

        print(
            f"Página {page_number}: "
            f"{len(records)} procesos"
        )

        all_records.extend(
            records
        )

        pages_processed += 1

    # --------------------------------------------------------
    # DEDUPLICACION
    # --------------------------------------------------------

    unique = {}

    for record in all_records:

        numero = (
            record[
                "numero_proceso"
            ]
            .strip()
        )

        unique[numero] = record

    records = list(
        unique.values()
    )

    print("")
    print(
        "Registros obtenidos:",
        len(all_records),
    )

    print(
        "Procesos únicos:",
        len(records),
    )

    return {
        "portal_total":
            portal_total,

        "page_size":
            page_size,

        "pages_expected":
            total_pages,

        "pages_processed":
            pages_processed,

        "raw_records":
            len(all_records),

        "records":
            records,
    }


# ============================================================
# DETALLE DE CADA PROCESO
# ============================================================

def enrich_process_details(
    browser_context,
    records,
):
    """
    Visita el detalle de cada proceso cuando el listado
    proporciona un link directo.

    Esto mejora la cobertura semántica porque no dependemos
    exclusivamente del nombre descriptivo del proceso.
    """

    detail_page = (
        browser_context
        .new_page()
    )

    detail_success = 0
    detail_failed = 0
    no_detail_url = 0

    total = len(records)

    for index, record in enumerate(
        records,
        start=1,
    ):

        print(
            f"Detalle {index}/{total}: "
            f"{record['numero_proceso']}"
        )

        process_url = (
            record.get(
                "process_url",
                "",
            )
        )

        record[
            "detail_status"
        ] = ""

        record[
            "detail_text"
        ] = ""

        if not process_url:

            no_detail_url += 1

            record[
                "detail_status"
            ] = "NO_DIRECT_URL"

            continue

        try:

            detail_page.goto(
                process_url,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )

            detail_page.wait_for_timeout(
                DETAIL_WAIT_MS
            )

            text = (
                detail_page
                .locator("body")
                .inner_text()
            )

            text = (
                normalize_multiline_text(
                    text
                )
            )

            if not text:

                raise RuntimeError(
                    "Detalle vacío"
                )

            record[
                "detail_text"
            ] = text[
                :MAX_DETAIL_TEXT
            ]

            record[
                "detail_status"
            ] = "OK"

            detail_success += 1

        except Exception as exc:

            detail_failed += 1

            record[
                "detail_status"
            ] = "ERROR"

            record[
                "detail_error"
            ] = clean_text(
                exc
            )[:500]

    detail_page.close()

    return {
        "detail_expected":
            total,

        "detail_success":
            detail_success,

        "detail_failed":
            detail_failed,

        "detail_no_direct_url":
            no_detail_url,
    }


# ============================================================
# SALIDA JSON
# ============================================================

def write_json(records):
    payload = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "source":
            "COMPR.AR - Compras Públicas Argentina",

        "source_url":
            SOURCE_URL,

        "universe":
            "Licitaciones de apertura próxima",

        "total_records":
            len(records),

        "data":
            records,
    }

    OUTPUT_JSON.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# SALIDA CSV
# ============================================================

def write_csv(records):
    if not records:

        OUTPUT_CSV.write_text(
            "sin_registros\n",
            encoding="utf-8",
        )

        return

    columns = []

    seen = set()

    for record in records:

        for key in record.keys():

            if key not in seen:

                seen.add(key)
                columns.append(key)

    with OUTPUT_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )

        writer.writeheader()

        writer.writerows(
            records
        )


# ============================================================
# METADATA
# ============================================================

def write_metadata(
    extraction_result,
    detail_result,
):
    portal_total = (
        extraction_result[
            "portal_total"
        ]
    )

    unique_records = len(
        extraction_result[
            "records"
        ]
    )

    json_records = (
        unique_records
    )

    csv_records = (
        unique_records
    )

    pages_complete = (
        extraction_result[
            "pages_processed"
        ]
        ==
        extraction_result[
            "pages_expected"
        ]
    )

    counts_match = (
        portal_total
        ==
        unique_records
        ==
        json_records
        ==
        csv_records
    )

    coverage_complete = (
        portal_total > 0
        and pages_complete
        and counts_match
    )

    detail_complete = (
        detail_result[
            "detail_success"
        ]
        ==
        detail_result[
            "detail_expected"
        ]
    )

    validation_status = (
        "OK"
        if coverage_complete
        else "ERROR"
    )

    metadata = {
        "extraction_timestamp":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source":
            "COMPR.AR - Compras Públicas Argentina",

        "source_url":
            SOURCE_URL,

        "universe":
            "Licitaciones de apertura próxima",

        "portal_total":
            portal_total,

        "page_size":
            extraction_result[
                "page_size"
            ],

        "pages_expected":
            extraction_result[
                "pages_expected"
            ],

        "pages_processed":
            extraction_result[
                "pages_processed"
            ],

        "raw_records":
            extraction_result[
                "raw_records"
            ],

        "unique_records":
            unique_records,

        "json_records":
            json_records,

        "csv_records":
            csv_records,

        "pages_complete":
            pages_complete,

        "counts_match":
            counts_match,

        "coverage_complete":
            coverage_complete,

        "detail_expected":
            detail_result[
                "detail_expected"
            ],

        "detail_success":
            detail_result[
                "detail_success"
            ],

        "detail_failed":
            detail_result[
                "detail_failed"
            ],

        "detail_no_direct_url":
            detail_result[
                "detail_no_direct_url"
            ],

        "detail_coverage_complete":
            detail_complete,

        "coverage_basis":
            "PORTAL_TOTAL_VS_FULL_PAGINATION",

        "validation_status":
            validation_status,
    }

    if coverage_complete:

        metadata[
            "note"
        ] = (
            "Cobertura estructural validada "
            "contra el total informado por "
            "COMPR.AR y el recorrido completo "
            "de todas las páginas."
        )

    else:

        metadata[
            "note"
        ] = (
            "No se pudo certificar la cobertura "
            "estructural del 100%."
        )

    OUTPUT_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "===================================="
    )

    print(
        "COMPR.AR - APERTURA PROXIMA"
    )

    print(
        "===================================="
    )

    with sync_playwright() as p:

        browser = (
            p.chromium.launch(
                headless=True
            )
        )

        context = (
            browser.new_context(
                locale="es-AR",

                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"
                ),
            )
        )

        page = (
            context.new_page()
        )

        print(
            "Abriendo COMPR.AR..."
        )

        page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=TIMEOUT_MS,
        )

        page.wait_for_timeout(
            3000
        )

        body = (
            page
            .locator("body")
            .inner_text()
        )

        if (
            "Licitaciones de apertura próxima"
            not in body
        ):

            raise RuntimeError(
                "No se encontró la pantalla "
                "de Licitaciones de apertura próxima."
            )

        # ====================================================
        # LISTADO COMPLETO
        # ====================================================

        extraction_result = (
            extract_all_processes(
                page
            )
        )

        records = (
            extraction_result[
                "records"
            ]
        )

        # ====================================================
        # VALIDACION ESTRUCTURAL TEMPRANA
        # ====================================================

        portal_total = (
            extraction_result[
                "portal_total"
            ]
        )

        if (
            len(records)
            != portal_total
        ):

            print("")
            print(
                "ERROR:"
            )

            print(
                "Portal:",
                portal_total,
            )

            print(
                "Procesos únicos:",
                len(records),
            )

            browser.close()

            sys.exit(1)

        # ====================================================
        # DETALLE
        # ====================================================

        detail_result = (
            enrich_process_details(
                context,
                records,
            )
        )

        browser.close()

    # ========================================================
    # ARCHIVOS
    # ========================================================

    write_json(
        records
    )

    write_csv(
        records
    )

    write_metadata(
        extraction_result,
        detail_result,
    )

    # ========================================================
    # RESUMEN
    # ========================================================

    print("")
    print(
        "===================================="
    )

    print(
        "VALIDACION FINAL"
    )

    print(
        "===================================="
    )

    print(
        "Portal:",
        portal_total,
    )

    print(
        "Procesos únicos:",
        len(records),
    )

    print(
        "Páginas esperadas:",
        extraction_result[
            "pages_expected"
        ],
    )

    print(
        "Páginas procesadas:",
        extraction_result[
            "pages_processed"
        ],
    )

    print(
        "Detalles OK:",
        detail_result[
            "detail_success"
        ],
    )

    print(
        "Detalles fallidos:",
        detail_result[
            "detail_failed"
        ],
    )

    print(
        "Sin URL directa:",
        detail_result[
            "detail_no_direct_url"
        ],
    )

    if (
        portal_total
        != len(records)
    ):

        print(
            "ERROR: cobertura incompleta."
        )

        sys.exit(1)

    print("")
    print(
        "COBERTURA ESTRUCTURAL "
        "100% VERIFICADA"
    )


if __name__ == "__main__":
    main()
