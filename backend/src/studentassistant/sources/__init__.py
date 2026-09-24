"""Source ingestion: capture processing, page transcription, textbook, PDF, web."""

from studentassistant.sources.pdf import (
    ImportedPage,
    ImportedPdf,
    PageRange,
    PageRangeError,
    PdfImportError,
    PdfTooLargeError,
    PdfUnreadableError,
    citation_source_id,
    import_pdf,
    original_page,
    parse_page_range,
    pdf_document_block,
    pdf_has_page,
    pdf_page_count,
)

__all__ = [
    "ImportedPage",
    "ImportedPdf",
    "PageRange",
    "PageRangeError",
    "PdfImportError",
    "PdfTooLargeError",
    "PdfUnreadableError",
    "citation_source_id",
    "import_pdf",
    "original_page",
    "parse_page_range",
    "pdf_document_block",
    "pdf_has_page",
    "pdf_page_count",
]
