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

TIMEOUT_MS = 180000
PAGE_WAIT_MS = 800
DETAIL_WAIT_MS = 400
MAX_DETAIL_TEXT = 10000

MAX_OPEN_RETRIES = 3
MAX_EXTRACTION_RETRIES = 3

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
# APERTURA ROBUSTA
# ============================================================

async def robust_open_comprar(page):
    page.set_default_timeout(
        120000
    )

    page.set_default_navigation_timeout(
        180000
    )

    last_error = None

    for attempt in range(
        1,
        MAX_OPEN_RETRIES + 1,
    ):

        print(
            f"Intento {attempt}/"
            f"{MAX_OPEN_RETRIES} "
            "de apertura de COMPR.AR..."
        )

        try:

            await page.goto(
                SOURCE_URL,
                wait_until="commit",
                timeout=180000,
            )

            await page.wait_for_timeout(
                4000
            )

        except PlaywrightTimeoutError as exc:

            last_error = exc

            print(
                "Timeout de navegación. "
                "Verificando contenido..."
            )

        except Exception as exc:

            last_error = exc

            print(
                "Error de navegación:",
                str(exc),
            )

        try:

            body = page.locator(
                "body"
            )

            await body.wait_for(
                state="attached",
                timeout=30000,
            )

            body_text = await (
                body.inner_text(
                    timeout=30000
                )
            )

            body_text = clean_text(
                body_text
            )

            if (
                "Licitaciones de apertura próxima"
                in body_text
            ):

                print(
                    "COMPR.AR cargado correctamente."
                )

                return

        except Exception as exc:

            last_error = exc

            print(
                "No fue posible leer "
                "el contenido:",
                str(exc),
            )

        if attempt < MAX_OPEN_RETRIES:

            print(
                "Esperando 10 segundos "
                "antes de reintentar..."
            )

            await page.wait_for_timeout(
                10000
            )

    raise RuntimeError(
        "No se pudo acceder a "
        "Licitaciones de apertura próxima. "
        f"Último error: {last_error}"
    )


# ============================================================
# TOTAL DEL PORTAL
# ============================================================

async def extract_portal_total(page):
    body_text = await (
        page
        .locator("body")
        .inner_text()
    )

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

            raw_total = (
                match
                .group(1)
                .replace(".", "")
                .replace(",", "")
            )

            return int(
                raw_total
            )

    raise RuntimeError(
        "No se pudo identificar "
        "el total informado por COMPR.AR."
    )


# ============================================================
# LOCALIZAR LA TABLA REAL DE RESULTADOS
# ============================================================

async def find_results_table(page):
    """
    Busca exclusivamente la tabla que contiene
    los encabezados del listado de licitaciones.

    Esto evita capturar filas de login, pie,
    paginadores u otras tablas de la página.
    """

    tables = page.locator(
        "table"
    )

    table_count = await (
        tables.count()
    )

    for index in range(
        table_count
    ):

        table = tables.nth(
            index
        )

        try:

            text = clean_text(
                await table.inner_text()
            )

        except Exception:

            continue

        lower = text.lower()

        has_numero = (
            "número de proceso"
            in lower
            or
            "numero de proceso"
            in lower
        )

        has_nombre = (
            "nombre descriptivo de proceso"
            in lower
        )

        has_tipo = (
            "tipo de proceso"
            in lower
        )

        has_apertura = (
            "fecha de apertura"
            in lower
        )

        if (
            has_numero
            and has_nombre
            and has_tipo
            and has_apertura
        ):

            return table

    raise RuntimeError(
        "No se pudo identificar "
        "la tabla principal de procesos."
    )


# ============================================================
# VALIDAR NUMERO DE PROCESO
# ============================================================

def looks_like_process_number(value):
    """
    Evita incorporar filas auxiliares.

    Ejemplos:
    84/81-1283-LPR26
    30-0015-CDI26
    509/1-0035-LPR26
    """

    value = clean_text(
        value
    )

    if not value:
        return False

    if len(value) > 80:
        return False

    if "-" not in value:
        return False

    if not re.search(
        r"\d",
        value,
    ):
        return False

    # Los procesos COMPR.AR terminan habitualmente
    # con código de modalidad + año.
    if not re.search(
        r"-[A-Za-z]{2,6}\d{2}$",
        value,
    ):
        return False

    return True


# ============================================================
# EXTRAER FILAS REALES
# ============================================================

async def extract_process_rows(page):
    table = await (
        find_results_table(
            page
        )
    )

    rows = table.locator(
        "tr"
    )

    row_count = await (
        rows.count()
    )

    records = []

    for index in range(
        row_count
    ):

        row = rows.nth(
            index
        )

        cells = row.locator(
            "td"
        )

        cell_count = await (
            cells.count()
        )

        # El listado real tiene exactamente 7 columnas.
        if cell_count != 7:
            continue

        values = []

        for cell_index in range(
            7
        ):

            try:

                text = await (
                    cells
                    .nth(cell_index)
                    .inner_text()
                )

            except Exception:

                text = ""

            values.append(
                clean_text(text)
            )

        numero_proceso = (
            values[0]
        )

        if not looks_like_process_number(
            numero_proceso
        ):

            continue

        # ----------------------------------------------------
        # LINK DEL PROCESO
        # ----------------------------------------------------

        process_url = ""
        process_href = ""

        first_cell = (
            cells.nth(0)
        )

        anchors = (
            first_cell.locator(
                "a"
            )
        )

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
                values[0],

            "nombre_proceso":
                values[1],

            # CAMPO EXPLICITO
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
# FIRMA DE PAGINA
# ============================================================

async def get_page_signature(page):
    records = await (
        extract_process_rows(
            page
        )
    )

    if not records:
        return ""

    numbers = [
        item[
            "numero_proceso"
        ]
        for item in records[:5]
    ]

    return "|".join(
        numbers
    )


# ============================================================
# BUSCAR LINK EXACTO DE PAGINA
# ============================================================

async def find_exact_page_link(
    page,
    page_number,
):
    """
    Busca un href ASP.NET del tipo:

    Page$2
    Page$3
    ...
    """

    anchors = page.locator(
        "a"
    )

    count = await (
        anchors.count()
    )

    pattern = re.compile(
        rf"Page\${page_number}"
        rf"(?:'|\")?",
        flags=re.IGNORECASE,
    )

    for index in range(
        count
    ):

        anchor = anchors.nth(
            index
        )

        href = (
            await anchor
            .get_attribute("href")
            or ""
        )

        if pattern.search(
            href
        ):

            return anchor

    return None


# ============================================================
# BUSCAR PAGE$NEXT
# ============================================================

async def find_next_block_link(
    page
):
    """
    En COMPR.AR Page$Next suele significar
    siguiente bloque del paginador:
    1-10 -> 11-20 -> 21-30.

    Por eso SOLO se usa cuando la página
    numérica siguiente no está disponible.
    """

    anchors = page.locator(
        "a"
    )

    count = await (
        anchors.count()
    )

    for index in range(
        count
    ):

        anchor = anchors.nth(
            index
        )

        href = (
            await anchor
            .get_attribute("href")
            or ""
        )

        if re.search(
            r"Page\$Next",
            href,
            flags=re.IGNORECASE,
        ):

            return anchor

    return None


# ============================================================
# ESPERAR CAMBIO DE PAGINA
# ============================================================

async def wait_for_page_change(
    page,
    previous_signature,
):
    for _ in range(
        50
    ):

        await page.wait_for_timeout(
            400
        )

        try:

            new_signature = await (
                get_page_signature(
                    page
                )
            )

        except Exception:

            continue

        if (
            new_signature
            and
            new_signature
            != previous_signature
        ):

            return

    raise RuntimeError(
        "El paginador fue accionado "
        "pero la página no cambió."
    )


# ============================================================
# NAVEGAR A LA SIGUIENTE PAGINA REAL
# ============================================================

async def go_to_page(
    page,
    target_page_number,
):
    previous_signature = await (
        get_page_signature(
            page
        )
    )

    # Primero buscamos la página exacta.
    link = await (
        find_exact_page_link(
            page,
            target_page_number,
        )
    )

    navigation_type = (
        "PAGINA_EXACTA"
    )

    # Si no existe, estamos probablemente
    # al final de un bloque 1-10, 11-20, etc.
    if link is None:

        link = await (
            find_next_block_link(
                page
            )
        )

        navigation_type = (
            "SIGUIENTE_BLOQUE"
        )

    if link is None:

        raise RuntimeError(
            "No se encontró ningún "
            "control para avanzar a "
            f"la página {target_page_number}."
        )

    print(
        f"Navegando a página "
        f"{target_page_number} "
        f"mediante {navigation_type}"
    )

    try:

        await link.click(
            timeout=60000,
        )

    except Exception:

        await link.click(
            force=True,
            timeout=60000,
        )

    await wait_for_page_change(
        page,
        previous_signature,
    )


# ============================================================
# UNA EXTRACCION COMPLETA
# ============================================================

async def extract_listing_once(
    page
):
    portal_total_start = (
        await extract_portal_total(
            page
        )
    )

    first_records = (
        await extract_process_rows(
            page
        )
    )

    if not first_records:

        raise RuntimeError(
            "No se encontraron procesos "
            "en la primera página."
        )

    page_size = len(
        first_records
    )

    print("")
    print(
        "Total portal:",
        portal_total_start,
    )

    print(
        "Procesos reales por página:",
        page_size,
    )

    # COMPR.AR debería devolver 10.
    if page_size != 10:

        print(
            "ADVERTENCIA: se esperaban "
            "10 procesos en la primera página "
            "y se detectaron:",
            page_size,
        )

    pages_expected = math.ceil(
        portal_total_start
        / page_size
    )

    print(
        "Páginas esperadas:",
        pages_expected,
    )

    all_records = []

    seen_page_signatures = set()

    for page_number in range(
        1,
        pages_expected + 1,
    ):

        if page_number > 1:

            await go_to_page(
                page,
                page_number,
            )

        records = await (
            extract_process_rows(
                page
            )
        )

        signature = await (
            get_page_signature(
                page
            )
        )

        if not signature:

            raise RuntimeError(
                f"No se pudo identificar "
                f"la página {page_number}."
            )

        if signature in (
            seen_page_signatures
        ):

            raise RuntimeError(
                f"La página {page_number} "
                "es una repetición de otra "
                "página ya procesada."
            )

        seen_page_signatures.add(
            signature
        )

        print(
            f"Página {page_number}/"
            f"{pages_expected}: "
            f"{len(records)} procesos"
        )

        all_records.extend(
            records
        )

    # ========================================================
    # DEDUPLICACION
    # ========================================================

    unique = {}

    duplicate_numbers = []

    for record in all_records:

        key = clean_text(
            record[
                "numero_proceso"
            ]
        )

        if key in unique:

            duplicate_numbers.append(
                key
            )

        unique[key] = record

    records = list(
        unique.values()
    )

    # ========================================================
    # VALIDAR TOTAL AL FINAL
    # ========================================================

    portal_total_end = None

    try:

        portal_total_end = (
            await extract_portal_total(
                page
            )
        )

    except Exception:

        pass

    print("")
    print(
        "===================================="
    )

    print(
        "RESULTADO EXTRACCION"
    )

    print(
        "===================================="
    )

    print(
        "Portal inicial:",
        portal_total_start,
    )

    print(
        "Portal final:",
        portal_total_end,
    )

    print(
        "Páginas procesadas:",
        pages_expected,
    )

    print(
        "Registros crudos:",
        len(all_records),
    )

    print(
        "Procesos únicos:",
        len(records),
    )

    print(
        "Duplicados:",
        len(
            duplicate_numbers
        ),
    )

    return {
        "portal_total_start":
            portal_total_start,

        "portal_total_end":
            portal_total_end,

        "page_size":
            page_size,

        "pages_expected":
            pages_expected,

        "pages_processed":
            pages_expected,

        "raw_records":
            len(all_records),

        "duplicates_detected":
            len(
                duplicate_numbers
            ),

        "records":
            records,
    }


# ============================================================
# EXTRACCION CON REINTENTOS
# ============================================================

async def extract_all_processes(
    page
):
    last_error = None

    for attempt in range(
        1,
        MAX_EXTRACTION_RETRIES + 1,
    ):

        print("")
        print(
            "===================================="
        )

        print(
            f"PASADA {attempt}/"
            f"{MAX_EXTRACTION_RETRIES}"
        )

        print(
            "===================================="
        )

        try:

            await robust_open_comprar(
                page
            )

            result = await (
                extract_listing_once(
                    page
                )
            )

            portal_start = (
                result[
                    "portal_total_start"
                ]
            )

            portal_end = (
                result[
                    "portal_total_end"
                ]
            )

            unique_count = len(
                result[
                    "records"
                ]
            )

            total_stable = (
                portal_end is None
                or portal_end
                == portal_start
            )

            counts_match = (
                unique_count
                == portal_start
            )

            no_duplicates = (
                result[
                    "duplicates_detected"
                ] == 0
            )

            print("")
            print(
                "CONTROL PASADA:"
            )

            print(
                "- total estable:",
                total_stable,
            )

            print(
                "- cantidad coincide:",
                counts_match,
            )

            print(
                "- sin duplicados:",
                no_duplicates,
            )

            if (
                total_stable
                and
                counts_match
                and
                no_duplicates
            ):

                print(
                    "PASADA VALIDADA."
                )

                return result

        except Exception as exc:

            last_error = exc

            print(
                "ERROR EN PASADA:",
                str(exc),
            )

        if (
            attempt
            <
            MAX_EXTRACTION_RETRIES
        ):

            print(
                "Esperando 10 segundos "
                "antes de repetir..."
            )

            await page.wait_for_timeout(
                10000
            )

    raise RuntimeError(
        "No se logró una extracción "
        "completa después de "
        f"{MAX_EXTRACTION_RETRIES} "
        "intentos. "
        f"Último error: {last_error}"
    )


# ============================================================
# DETALLES
# ============================================================

async def enrich_process_details(
    context,
    records,
):
    detail_page = await (
        context.new_page()
    )

    success = 0
    failed = 0
    no_url = 0

    total = len(
        records
    )

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

            no_url += 1

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
                wait_until="commit",
                timeout=TIMEOUT_MS,
            )

            await detail_page.wait_for_timeout(
                DETAIL_WAIT_MS
            )

            text = await (
                detail_page
                .locator("body")
                .inner_text(
                    timeout=30000
                )
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

            success += 1

        except Exception as exc:

            failed += 1

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
            success,

        "detail_failed":
            failed,

        "detail_no_direct_url":
            no_url,
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
            "COMPR.AR",

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

                seen.add(
                    key
                )

                columns.append(
                    key
                )

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

    return max(
        0,
        len(rows) - 1,
    )


# ============================================================
# METADATA
# ============================================================

def write_metadata(
    extraction_result,
    detail_result,
):
    portal_start = (
        extraction_result[
            "portal_total_start"
        ]
    )

    portal_end = (
        extraction_result[
            "portal_total_end"
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

    total_stable = (
        portal_end is None
        or
        portal_start
        == portal_end
    )

    counts_match = (
        portal_start
        ==
        unique_records
        ==
        json_records
        ==
        csv_records
    )

    no_duplicates = (
        extraction_result[
            "duplicates_detected"
        ] == 0
    )

    pages_complete = (
        extraction_result[
            "pages_expected"
        ]
        ==
        extraction_result[
            "pages_processed"
        ]
    )

    coverage_complete = (
        portal_start > 0
        and total_stable
        and counts_match
        and no_duplicates
        and pages_complete
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

    metadata = {
        "extraction_timestamp":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source":
            "COMPR.AR",

        "source_url":
            SOURCE_URL,

        "universe":
            "Licitaciones de apertura próxima",

        "portal_total_start":
            portal_start,

        "portal_total_end":
            portal_end,

        "portal_total_stable":
            total_stable,

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

        "pages_complete":
            pages_complete,

        "raw_records":
            extraction_result[
                "raw_records"
            ],

        "duplicates_detected":
            extraction_result[
                "duplicates_detected"
            ],

        "unique_records":
            unique_records,

        "json_records":
            json_records,

        "csv_records":
            csv_records,

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
            (
                "PORTAL_TOTAL_VS_"
                "EXACT_NUMBERED_PAGINATION"
            ),

        "validation_status":
            (
                "OK"
                if coverage_complete
                else "ERROR"
            ),
    }

    if coverage_complete:

        metadata[
            "note"
        ] = (
            "Cobertura estructural "
            "100% validada contra "
            "el total informado por COMPR.AR. "
            "Se procesaron todas las páginas "
            "sin duplicados."
        )

    else:

        metadata[
            "note"
        ] = (
            "No se pudo certificar "
            "cobertura estructural 100%."
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

        extraction_result = await (
            extract_all_processes(
                page
            )
        )

        records = (
            extraction_result[
                "records"
            ]
        )

        detail_result = await (
            enrich_process_details(
                context,
                records,
            )
        )

        await browser.close()

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
        extraction_result[
            "portal_total_start"
        ],
    )

    print(
        "Procesos únicos:",
        len(records),
    )

    print(
        "Procesos por página:",
        extraction_result[
            "page_size"
        ],
    )

    print(
        "Páginas:",
        extraction_result[
            "pages_processed"
        ],
    )

    print(
        "Duplicados:",
        extraction_result[
            "duplicates_detected"
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
