import asyncio
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    async_playwright,
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

# Espera entre navegaciones para no sobrecargar COMPR.AR
PAGE_WAIT_MS = 800
DETAIL_WAIT_MS = 300

# Limite del texto capturado en cada detalle
MAX_DETAIL_TEXT = 10000

# Permite leer celdas CSV extensas
csv.field_size_limit(sys.maxsize)


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
# TOTAL DEL PORTAL
# ============================================================

async def extract_portal_total(page):
    body_text = await page.locator(
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
# EXTRAER FILAS DE UNA PAGINA
# ============================================================

async def extract_process_rows(page):
    rows = page.locator("tr")

    row_count = await rows.count()

    records = []

    for index in range(row_count):

        row = rows.nth(index)
        cells = row.locator("td")

        cell_count = await cells.count()

        if cell_count < 7:
            continue

        values = []

        for cell_index in range(7):

            text = await (
                cells
                .nth(cell_index)
                .inner_text()
            )

            values.append(
                clean_text(text)
            )

        numero_proceso = values[0]

        if not numero_proceso:
            continue

        if (
            "número de proceso"
            in numero_proceso.lower()
        ):
            continue

        if (
            "numero de proceso"
            in numero_proceso.lower()
        ):
            continue

        # ----------------------------------------------------
        # LINK / REFERENCIA AL DETALLE
        # ----------------------------------------------------

        process_url = ""
        process_href = ""

        first_cell = cells.nth(0)

        anchors = first_cell.locator("a")

        if await anchors.count() > 0:

            href = await (
                anchors
                .first
                .get_attribute("href")
            )

            if href:

                process_href = (
                    href.strip()
                )

                if not (
                    process_href
                    .lower()
                    .startswith("javascript:")
                ):

                    process_url = urljoin(
                        page.url,
                        process_href,
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

            "process_href":
                process_href,
        }

        records.append(
            record
        )

    return records


# ============================================================
# DETECTAR CONTROL DE PAGINACION ASP.NET
# ============================================================

async def find_pager_target(page):
    anchors = page.locator("a")

    anchor_count = await anchors.count()

    for index in range(anchor_count):

        href = await (
            anchors
            .nth(index)
            .get_attribute("href")
        )

        href = href or ""

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


# ============================================================
# IR A UNA PAGINA ESPECIFICA
# ============================================================

async def go_to_page_number(
    page,
    pager_target,
    page_number,
):
    if page_number == 1:
        return

    before_records = (
        await extract_process_rows(
            page
        )
    )

    previous_first = ""

    if before_records:

        previous_first = (
            before_records[0]
            ["numero_proceso"]
        )

    print(
        f"Abriendo página "
        f"{page_number}"
    )

    # Ejecutamos el mismo postback que utiliza
    # internamente el paginador ASP.NET.
    await page.evaluate(
        """
        ([target, argument]) => {
            if (
                typeof window.__doPostBack
                !== 'function'
            ) {
                throw new Error(
                    '__doPostBack no está disponible'
                );
            }

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

    # Esperamos a que el postback termine.
    try:

        await page.wait_for_load_state(
            "domcontentloaded",
            timeout=30000,
        )

    except PlaywrightTimeoutError:

        pass

    await page.wait_for_timeout(
        PAGE_WAIT_MS
    )

    new_records = (
        await extract_process_rows(
            page
        )
    )

    if not new_records:

        raise RuntimeError(
            f"La página {page_number} "
            "no devolvió procesos."
        )

    new_first = (
        new_records[0]
        ["numero_proceso"]
    )

    if (
        previous_first
        and new_first == previous_first
    ):

        raise RuntimeError(
            f"La paginación no avanzó "
            f"a la página {page_number}."
        )


# ============================================================
# EXTRAER TODO EL LISTADO
# ============================================================

async def extract_all_processes(page):
    portal_total = (
        await extract_portal_total(
            page
        )
    )

    print("")
    print(
        "Total informado por COMPR.AR:",
        portal_total,
    )

    first_page_records = (
        await extract_process_rows(
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
        portal_total
        / page_size
    )

    print(
        "Páginas esperadas:",
        total_pages,
    )

    pager_target = (
        await find_pager_target(
            page
        )
    )

    print(
        "Control paginador:",
        pager_target,
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

            print("")
            print(
                f"Abriendo página "
                f"{page_number}/"
                f"{total_pages}"
            )

            await go_to_page_number(
                page,
                pager_target,
                page_number,
            )

        records = (
            await extract_process_rows(
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

    # ========================================================
    # DEDUPLICACION
    # ========================================================

    unique = {}

    for record in all_records:

        numero = clean_text(
            record[
                "numero_proceso"
            ]
        )

        if not numero:
            continue

        unique[numero] = record

    records = list(
        unique.values()
    )

    print("")
    print(
        "===================================="
    )

    print(
        "RESULTADO LISTADO"
    )

    print(
        "===================================="
    )

    print(
        "Registros crudos:",
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
# ENRIQUECIMIENTO DE DETALLES
# ============================================================

async def enrich_process_details(
    context,
    records,
):
    detail_page = await (
        context.new_page()
    )

    detail_success = 0
    detail_failed = 0
    no_direct_url = 0

    total = len(records)

    for index, record in enumerate(
        records,
        start=1,
    ):

        process_url = clean_text(
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

            no_direct_url += 1

            record[
                "detail_status"
            ] = "NO_DIRECT_URL"

            continue

        print(
            f"Detalle {index}/"
            f"{total}: "
            f"{record['numero_proceso']}"
        )

        try:

            await detail_page.goto(
                process_url,
                wait_until=(
                    "domcontentloaded"
                ),
                timeout=TIMEOUT_MS,
            )

            await detail_page.wait_for_timeout(
                DETAIL_WAIT_MS
            )

            body_text = await (
                detail_page
                .locator("body")
                .inner_text()
            )

            body_text = (
                normalize_multiline_text(
                    body_text
                )
            )

            if not body_text:

                raise RuntimeError(
                    "Detalle vacío"
                )

            record[
                "detail_text"
            ] = body_text[
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

    await detail_page.close()

    return {
        "detail_expected":
            total,

        "detail_success":
            detail_success,

        "detail_failed":
            detail_failed,

        "detail_no_direct_url":
            no_direct_url,
    }


# ============================================================
# JSON
# ============================================================

def write_json(records):
    payload = {
        "generated_at":
            datetime.now(
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
# CSV
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


def count_csv_records():
    if not OUTPUT_CSV.exists():
        return 0

    with OUTPUT_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.reader(
            file
        )

        rows = list(
            reader
        )

    if len(rows) <= 1:
        return 0

    return len(rows) - 1


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
        count_csv_records()
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

    detail_coverage_complete = (
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

    if coverage_complete:

        note = (
            "Cobertura estructural validada "
            "contra el total informado por "
            "COMPR.AR y el recorrido completo "
            "de todas las páginas."
        )

    else:

        note = (
            "No se pudo certificar cobertura "
            "estructural 100%."
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
            detail_coverage_complete,

        "coverage_basis":
            "PORTAL_TOTAL_VS_FULL_PAGINATION",

        "validation_status":
            validation_status,

        "note":
            note,
    }

    OUTPUT_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# MAIN ASYNC
# ============================================================

async def main():
    print(
        "===================================="
    )

    print(
        "COMPR.AR - APERTURA PROXIMA"
    )

    print(
        "===================================="
    )

    async with async_playwright() as p:

        browser = await (
            p.chromium.launch(
                headless=True
            )
        )

        context = await (
            browser.new_context(
                locale="es-AR",

                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; "
                    "Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/130.0 "
                    "Safari/537.36"
                ),
            )
        )

        page = await (
            context.new_page()
        )

        print(
            "Abriendo COMPR.AR..."
        )

        await page.goto(
            SOURCE_URL,
            wait_until="domcontentloaded",
            timeout=TIMEOUT_MS,
        )

        await page.wait_for_timeout(
            3000
        )

        body_text = await (
            page
            .locator("body")
            .inner_text()
        )

        if (
            "Licitaciones de apertura próxima"
            not in body_text
        ):

            raise RuntimeError(
                "No se encontró la pantalla "
                "de Licitaciones de apertura próxima."
            )

        # ====================================================
        # LISTADO
        # ====================================================

        extraction_result = (
            await extract_all_processes(
                page
            )
        )

        records = (
            extraction_result[
                "records"
            ]
        )

        portal_total = (
            extraction_result[
                "portal_total"
            ]
        )

        # ====================================================
        # VALIDACION DEL LISTADO
        # ====================================================

        if (
            len(records)
            != portal_total
        ):

            print("")
            print(
                "ERROR DE COBERTURA"
            )

            print(
                "Total portal:",
                portal_total,
            )

            print(
                "Procesos únicos:",
                len(records),
            )

            await browser.close()

            sys.exit(1)

        # ====================================================
        # DETALLES
        # ====================================================

        detail_result = (
            await enrich_process_details(
                context,
                records,
            )
        )

        await browser.close()

    # ========================================================
    # SALIDAS
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
    # RESULTADO
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
        "Total portal:",
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
        "Detalles esperados:",
        detail_result[
            "detail_expected"
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


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    asyncio.run(
        main()
    )
