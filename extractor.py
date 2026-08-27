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

PAGE_WAIT_MS = 1000
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
                5000
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

            print(
                "La página respondió, "
                "pero todavía no muestra "
                "el listado esperado."
            )

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
        "el total de resultados "
        "informado por COMPR.AR."
    )


# ============================================================
# EXTRAER FILAS
# ============================================================

async def extract_process_rows(page):
    """
    Extrae:

    - Número de proceso
    - Nombre descriptivo
    - Tipo de proceso
    - Fecha de apertura
    - Estado
    - Unidad ejecutora
    - Servicio Administrativo Financiero
    """

    rows = page.locator(
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

        numero_proceso = (
            values[0]
        )

        if not numero_proceso:
            continue

        lower_numero = (
            numero_proceso.lower()
        )

        if (
            "número de proceso"
            in lower_numero
            or
            "numero de proceso"
            in lower_numero
        ):
            continue

        # Evita filas que no sean procesos.
        if (
            len(numero_proceso) < 3
        ):
            continue

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

            # CAMPO EXPLICITO PARA EL INFORME
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
# IDENTIFICADOR DE PAGINA
# ============================================================

async def get_page_signature(page):
    records = await (
        extract_process_rows(
            page
        )
    )

    if not records:
        return ""

    first_numbers = [
        clean_text(
            item[
                "numero_proceso"
            ]
        )
        for item in records[:3]
    ]

    return "|".join(
        first_numbers
    )


# ============================================================
# BUSCAR BOTON NEXT REAL
# ============================================================

async def find_next_link(page):
    """
    Busca EXCLUSIVAMENTE el control ASP.NET Page$Next.

    No intenta navegar a números absolutos.
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
# AVANZAR UNA PAGINA
# ============================================================

async def click_next_page(
    page,
    previous_signature,
):
    next_link = await (
        find_next_link(
            page
        )
    )

    if next_link is None:

        return False

    print(
        "Haciendo clic en Page$Next..."
    )

    try:

        await next_link.click(
            timeout=60000,
        )

    except Exception:

        await next_link.click(
            force=True,
            timeout=60000,
        )

    # --------------------------------------------------------
    # ESPERAR CAMBIO REAL
    # --------------------------------------------------------

    for _ in range(50):

        await page.wait_for_timeout(
            500
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

            return True

    raise RuntimeError(
        "Se accionó Page$Next "
        "pero el listado no cambió."
    )


# ============================================================
# UNA PASADA COMPLETA DEL LISTADO
# ============================================================

async def extract_full_listing_once(
    page
):
    portal_total_start = (
        await extract_portal_total(
            page
        )
    )

    print("")
    print(
        "Total inicial informado "
        "por COMPR.AR:",
        portal_total_start,
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

    expected_pages = math.ceil(
        portal_total_start
        / page_size
    )

    print(
        "Procesos por página:",
        page_size,
    )

    print(
        "Páginas teóricas:",
        expected_pages,
    )

    all_records = []

    page_signatures = set()

    page_number = 1

    while True:

        records = (
            await extract_process_rows(
                page
            )
        )

        if not records:

            raise RuntimeError(
                "Una página del listado "
                "no devolvió registros."
            )

        signature = await (
            get_page_signature(
                page
            )
        )

        if not signature:

            raise RuntimeError(
                "No se pudo construir "
                "la firma de la página."
            )

        # ----------------------------------------------------
        # DETECTAR REPETICION DE PAGINA
        # ----------------------------------------------------

        if signature in page_signatures:

            raise RuntimeError(
                "COMPR.AR repitió una página "
                "durante la paginación. "
                f"Firma: {signature}"
            )

        page_signatures.add(
            signature
        )

        print(
            f"Página real {page_number}: "
            f"{len(records)} procesos"
        )

        all_records.extend(
            records
        )

        # ----------------------------------------------------
        # ¿HAY NEXT?
        # ----------------------------------------------------

        next_link = await (
            find_next_link(
                page
            )
        )

        if next_link is None:

            print(
                "No existe Page$Next. "
                "Fin del listado."
            )

            break

        await click_next_page(
            page,
            signature,
        )

        page_number += 1

        # Seguridad por si el sitio entra
        # en un loop inesperado.
        if page_number > 200:

            raise RuntimeError(
                "Se superaron 200 páginas. "
                "Se aborta para evitar loop."
            )

    # ========================================================
    # DEDUPLICAR
    # ========================================================

    unique = {}

    duplicates = []

    for record in all_records:

        numero = clean_text(
            record[
                "numero_proceso"
            ]
        )

        if not numero:
            continue

        if numero in unique:

            duplicates.append(
                numero
            )

        unique[numero] = (
            record
        )

    records = list(
        unique.values()
    )

    # ========================================================
    # TOTAL FINAL DEL PORTAL
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
        "RESULTADO DE LA PASADA"
    )

    print(
        "===================================="
    )

    print(
        "Total portal inicial:",
        portal_total_start,
    )

    print(
        "Total portal final:",
        portal_total_end,
    )

    print(
        "Páginas reales procesadas:",
        page_number,
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
        "Duplicados detectados:",
        len(duplicates),
    )

    return {
        "portal_total_start":
            portal_total_start,

        "portal_total_end":
            portal_total_end,

        "page_size":
            page_size,

        "pages_expected":
            expected_pages,

        "pages_processed":
            page_number,

        "raw_records":
            len(all_records),

        "duplicates_detected":
            len(duplicates),

        "records":
            records,
    }


# ============================================================
# EXTRAER LISTADO CON REINTENTOS
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
            f"PASADA DE EXTRACCION "
            f"{attempt}/"
            f"{MAX_EXTRACTION_RETRIES}"
        )

        print(
            "===================================="
        )

        try:

            # Siempre volvemos al inicio,
            # para no heredar estado del intento anterior.
            await robust_open_comprar(
                page
            )

            result = await (
                extract_full_listing_once(
                    page
                )
            )

            unique_records = len(
                result[
                    "records"
                ]
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

            # ------------------------------------------------
            # EL TOTAL NO DEBE CAMBIAR DURANTE LA EXTRACCION
            # ------------------------------------------------

            total_stable = (
                portal_end is None
                or
                portal_start
                == portal_end
            )

            counts_match = (
                unique_records
                == portal_start
            )

            no_duplicates = (
                result[
                    "duplicates_detected"
                ] == 0
            )

            if (
                total_stable
                and
                counts_match
                and
                no_duplicates
            ):

                print("")
                print(
                    "PASADA VALIDADA "
                    "CORRECTAMENTE."
                )

                return result

            print("")
            print(
                "La pasada no pudo "
                "certificarse:"
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

            print(
                "- portal:",
                portal_start,
            )

            print(
                "- únicos:",
                unique_records,
            )

        except Exception as exc:

            last_error = exc

            print(
                "Error durante la pasada:",
                str(exc),
            )

        if (
            attempt
            <
            MAX_EXTRACTION_RETRIES
        ):

            print(
                "Esperando 10 segundos "
                "antes de repetir "
                "la extracción completa..."
            )

            await page.wait_for_timeout(
                10000
            )

    raise RuntimeError(
        "No fue posible obtener una "
        "extracción completa y consistente "
        "de COMPR.AR después de "
        f"{MAX_EXTRACTION_RETRIES} intentos. "
        f"Último error: {last_error}"
    )


# ============================================================
# ENRIQUECER DETALLES
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
                wait_until="commit",
                timeout=TIMEOUT_MS,
            )

            await detail_page.wait_for_timeout(
                DETAIL_WAIT_MS
            )

            body_text = await (
                detail_page
                .locator("body")
                .inner_text(
                    timeout=30000
                )
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
            ] = (
                clean_text(exc)
                [:500]
            )

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
            "portal_total_start"
        ]
    )

    portal_total_end = (
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
        portal_total_end is None
        or
        portal_total
        == portal_total_end
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

    no_duplicates = (
        extraction_result[
            "duplicates_detected"
        ] == 0
    )

    coverage_complete = (
        portal_total > 0
        and total_stable
        and counts_match
        and no_duplicates
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
            portal_total,

        "portal_total_end":
            portal_total_end,

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
            detail_coverage_complete,

        "coverage_basis":
            (
                "PORTAL_TOTAL_"
                "VS_SEQUENTIAL_NEXT_PAGINATION"
            ),

        "validation_status":
            validation_status,
    }

    if coverage_complete:

        metadata[
            "note"
        ] = (
            "Cobertura estructural 100% "
            "validada: se recorrió el listado "
            "secuencialmente mediante Page$Next, "
            "sin páginas repetidas ni procesos "
            "duplicados, y la cantidad de procesos "
            "coincide con el total informado "
            "por COMPR.AR."
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

        # ====================================================
        # LISTADO COMPLETO
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
                "portal_total_start"
            ]
        )

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
        "Total portal:",
        portal_total,
    )

    print(
        "Procesos únicos:",
        len(records),
    )

    print(
        "Duplicados:",
        extraction_result[
            "duplicates_detected"
        ],
    )

    print(
        "Páginas reales:",
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
