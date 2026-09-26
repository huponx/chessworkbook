import argparse
import logging
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus.doctemplate import ActionFlowable
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
)
from reportlab.platypus.tableofcontents import TableOfContents

from workbook_io import (
    OUT_DIR,
    FONTS_DIR,
    ROOT_DIR,
    chapter_description,
    chapter_display_name,
    item_note,
    item_title,
    sorted_chapters,
    sorted_items,
)

PAGE_WIDTH, PAGE_HEIGHT = A4

INNER_MARGIN = 60
OUTER_MARGIN = 20
TOP_MARGIN = 40
BOTTOM_MARGIN = 30
IMAGE_SIZE = 160
IMAGE_GAP = 10

CONTENT_PDF = OUT_DIR / "Chess Workbook - Content.pdf"
TOC_PDF = OUT_DIR / "Chess Workbook - TOC.pdf"
COMBINED_PDF = OUT_DIR / "Chess Workbook.pdf"


def register_fonts() -> None:
    for name, filename in (
        ("NotoSans", "NotoSans-Regular.ttf"),
        ("NotoSans-Bold", "NotoSans-Bold.ttf"),
        ("NotoSans-Italic", "NotoSans-Italic.ttf"),
        ("NotoSans-BoldItalic", "NotoSans-BoldItalic.ttf"),
    ):
        pdfmetrics.registerFont(TTFont(name, str(FONTS_DIR / filename)))


def para(text: str, style) -> Paragraph:
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


class ChessWorkbookDoc(BaseDocTemplate):
    """mode: 'combined' (TOC + content), 'content', or 'toc'."""

    def __init__(self, filename, *, mirror_margin: bool = True, mode: str = "combined"):
        self.mode = mode
        self.mirror_margin = mirror_margin
        self.current_chapter = ""
        self.current_chapter_title = ""
        self.chapter_start_pages: set[int] = set()
        self.page_chapters: dict[int, str] = {}
        self.chapter_page_numbers: list[int] = []
        self.content_start_page: int | None = None
        self._flow_page = 0
        self._first_flowable_on_page = True

        super().__init__(
            filename,
            leftMargin=OUTER_MARGIN,
            rightMargin=OUTER_MARGIN,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,
        )

        content_width = PAGE_WIDTH - INNER_MARGIN - OUTER_MARGIN
        content_height = PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN

        if mirror_margin:
            odd_x = INNER_MARGIN
            even_x = OUTER_MARGIN
        else:
            odd_x = even_x = (INNER_MARGIN + OUTER_MARGIN) / 2

        odd_frame = Frame(
            odd_x, BOTTOM_MARGIN, content_width, content_height, id="odd"
        )
        even_frame = Frame(
            even_x, BOTTOM_MARGIN, content_width, content_height, id="even"
        )

        self.addPageTemplates(
            [
                PageTemplate(
                    id="odd",
                    frames=[odd_frame],
                    onPageEnd=self.draw_header_footer,
                ),
                PageTemplate(
                    id="even",
                    frames=[even_frame],
                    onPageEnd=self.draw_header_footer,
                ),
            ]
        )

    def handle_pageBegin(self):
        # autoNextPageTemplate leaves the chosen template selected, so page 3
        # repeats page 2. Pick the frame from the page number instead.
        if self.mirror_margin:
            template_id = "odd" if (self.page + 1) % 2 == 1 else "even"
            for template in self.pageTemplates:
                if template.id == template_id:
                    self.pageTemplate = template
                    break
        super().handle_pageBegin()

    def beforeDocument(self):
        self.chapter_start_pages = set()
        self.page_chapters = {}
        self.chapter_page_numbers = []
        self.current_chapter = ""
        self.current_chapter_title = ""
        self.content_start_page = None
        self._flow_page = 0
        self._first_flowable_on_page = True

    def show_page_number(self, page: int) -> bool:
        if self.mode == "toc":
            return False
        if self.mode == "content":
            return True
        return self.content_start_page is not None and page >= self.content_start_page

    def chapter_for_page(self, page: int) -> str | None:
        if not self.page_chapters:
            return None
        eligible = [p for p in self.page_chapters if p <= page]
        if not eligible:
            return None
        return self.page_chapters.get(max(eligible))

    def current_chapter_header(self) -> str:
        return self.current_chapter_title

    @staticmethod
    def chapter_title_from_paragraph(text: str) -> str:
        return re.sub(r"^\d+\.\s+", "", text, count=1)

    def draw_header_footer(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("NotoSans", 9)

        page = canvas.getPageNumber()
        chapter_name = self.chapter_for_page(page) if self.mode != "toc" else None

        logging.debug(
            "HEADER page=%s chapter=%s suppress=%s mode=%s",
            page,
            chapter_name,
            page in self.chapter_start_pages,
            self.mode,
        )

        if (
            self.mode != "toc"
            and chapter_name
            and page not in self.chapter_start_pages
        ):
            canvas.drawCentredString(
                PAGE_WIDTH / 2,
                PAGE_HEIGHT - 20,
                chapter_name,
            )

        canvas.drawString(PAGE_WIDTH / 2 - 40, 20, "Mỡ Mỡ Chess Workbook")

        if self.show_page_number(page):
            if not self.mirror_margin:
                canvas.drawRightString(PAGE_WIDTH - OUTER_MARGIN, 20, str(page))
            elif page % 2 == 1:
                canvas.drawRightString(PAGE_WIDTH - OUTER_MARGIN, 20, str(page))
            else:
                canvas.drawString(OUTER_MARGIN, 20, str(page))

        canvas.restoreState()

    def _mark_page_flowable(self, flowable) -> bool | None:
        if isinstance(flowable, ActionFlowable):
            return None

        if self.page != self._flow_page:
            self._flow_page = self.page
            self._first_flowable_on_page = True
        first_on_page = self._first_flowable_on_page
        self._first_flowable_on_page = False
        return first_on_page

    def afterFlowable(self, flowable):
        if self.mode == "toc":
            return

        first_on_page = self._mark_page_flowable(flowable)
        if first_on_page is None:
            return

        if not isinstance(flowable, Paragraph):
            return

        style_name = flowable.style.name
        text = flowable.getPlainText()
        logging.debug(
            "page=%s style=%s first=%s text=%s",
            self.page,
            style_name,
            first_on_page,
            text[:80],
        )

        if style_name == "Item":
            if first_on_page:
                self.chapter_start_pages.add(self.page)
            return

        if style_name != "Chapter":
            return

        self.current_chapter = text
        self.current_chapter_title = self.chapter_title_from_paragraph(text)
        logging.debug("CHAPTER page=%s text=%s", self.page, text)

        if self.mode == "combined" and self.content_start_page is None:
            self.content_start_page = self.page

        self.chapter_start_pages.add(self.page)
        self.page_chapters[self.page] = self.current_chapter_title
        self.chapter_page_numbers.append(self.page)

        if self.mode != "combined":
            return

        key = "chapter_" + str(self.page) + "_" + text.replace(" ", "_")
        self.canv.bookmarkPage(key)
        self.notify("TOCEntry", (0, text, self.page, key))


def create_styles():
    styles = getSampleStyleSheet()

    chapter_style = ParagraphStyle(
        name="Chapter",
        parent=styles["Heading1"],
        fontName="NotoSans-Bold",
        fontSize=20,
        spaceAfter=0,
    )
    chapter_description_style = ParagraphStyle(
        name="ChapterDescription",
        parent=styles["BodyText"],
        fontName="NotoSans-Italic",
        fontSize=9,
        leading=12,
        textColor="#555555",
        spaceBefore=2,
        spaceAfter=8,
    )
    item_style = ParagraphStyle(
        name="Item",
        parent=styles["Heading3"],
        fontName="NotoSans",
        fontSize=10,
        spaceBefore=0,
        spaceAfter=0,
    )
    summary_style = ParagraphStyle(
        name="Summary",
        parent=styles["BodyText"],
        fontName="NotoSans-Italic",
        fontSize=9,
        textColor="#555555",
        leading=14,
        spaceBefore=0,
        spaceAfter=0,
    )
    toc_style = ParagraphStyle(
        name="TOCChapter",
        fontName="NotoSans",
        fontSize=12,
        leftIndent=20,
        firstLineIndent=-10,
        spaceBefore=4,
        leading=14,
    )
    toc_title_style = ParagraphStyle(
        name="TOCTitle",
        parent=styles["Title"],
        fontName="NotoSans-Bold",
    )
    image_number_style = ParagraphStyle(
        name="ImageNumber",
        parent=styles["BodyText"],
        alignment=1,
        fontName="NotoSans",
        fontSize=7,
        leading=10,
        spaceBefore=0,
        spaceAfter=0,
    )

    return {
        "chapter": chapter_style,
        "chapter_description": chapter_description_style,
        "item": item_style,
        "summary": summary_style,
        "toc": toc_style,
        "toc_title": toc_title_style,
        "image_number": image_number_style,
    }


def build_chapters_story(styles) -> list:
    story = []
    chapters = sorted_chapters()
    if not chapters:
        logging.warning("No chapters found in %s", ROOT_DIR)

    for chapter_index, chapter in enumerate(chapters):
        chapter_name = chapter_display_name(chapter)
        description = chapter_description(chapter)

        story.append(
            para(f"{chapter_index + 1}. {chapter_name}", styles["chapter"])
        )

        if description:
            story.append(para(description, styles["chapter_description"]))

        story.append(Spacer(1, 10))

        for item_index, item in enumerate(sorted_items(chapter), start=1):
            title = item_title(item)
            note = item_note(item)
            images = sorted((item / "cropped").glob("*.png"))

            block = [
                para(
                    f"{chapter_index + 1}.{item_index}. {title}",
                    styles["item"],
                ),
                Spacer(1, 6),
            ]

            rows = []
            current_images = []
            current_labels = []

            for image_index, image_file in enumerate(images, start=1):
                current_images.append(
                    Image(str(image_file), width=IMAGE_SIZE, height=IMAGE_SIZE)
                )
                current_labels.append(
                    Paragraph(str(image_index), styles["image_number"])
                )

                if len(current_images) == 3:
                    rows.append(current_images)
                    rows.append(current_labels)
                    current_images = []
                    current_labels = []

            if current_images:
                while len(current_images) < 3:
                    current_images.append("")
                    current_labels.append("")
                rows.append(current_images)
                rows.append(current_labels)

            if rows:
                table = Table(
                    rows,
                    colWidths=[IMAGE_SIZE + IMAGE_GAP] * 3,
                )
                table.setStyle(
                    [
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, 0), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
                        ("TOPPADDING", (0, 1), (-1, 1), 2),
                        ("BOTTOMPADDING", (0, 1), (-1, 1), 2),
                    ]
                )
                block.append(table)

                if note:
                    summary = Table(
                        [[para(note, styles["summary"])]],
                        colWidths=["100%"],
                    )
                    summary.setStyle(
                        [
                            ("LINEBEFORE", (0, 0), (0, -1), 1, colors.grey),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                            ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                        ]
                    )
                    block.append(summary)

            block.append(Spacer(1, 12))
            story.extend(block)

        if chapter_index < len(chapters) - 1:
            story.append(PageBreak())

    return story


def build_combined_story(styles) -> list:
    toc = TableOfContents()
    toc.levelStyles = [styles["toc"]]
    return [
        para("Mục lục", styles["toc_title"]),
        Spacer(1, 20),
        toc,
        PageBreak(),
        *build_chapters_story(styles),
    ]


def build_toc_story(styles, chapter_page_numbers: list[int]) -> tuple[list, TableOfContents]:
    toc = TableOfContents()
    toc.levelStyles = [styles["toc"]]
    chapters = sorted_chapters()

    for index, (chapter, page_number) in enumerate(
        zip(chapters, chapter_page_numbers, strict=False),
        start=1,
    ):
        toc.addEntry(
            0,
            f"{index}. {chapter_display_name(chapter)}",
            page_number,
        )

    story = [
        para("Mục lục", styles["toc_title"]),
        Spacer(1, 20),
        toc,
    ]
    return story, toc


def build_combined_pdf() -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    styles = create_styles()

    doc = ChessWorkbookDoc(str(COMBINED_PDF), mirror_margin=False, mode="combined")
    doc.multiBuild(build_combined_story(styles))
    return COMBINED_PDF


def build_split_pdfs() -> tuple[Path, Path]:
    OUT_DIR.mkdir(exist_ok=True)
    styles = create_styles()

    content_doc = ChessWorkbookDoc(str(CONTENT_PDF), mirror_margin=True, mode="content")
    content_doc.build(build_chapters_story(styles))
    chapter_pages = content_doc.chapter_page_numbers

    if len(chapter_pages) != len(sorted_chapters()):
        raise RuntimeError(
            f"Expected {len(sorted_chapters())} chapter pages, got {len(chapter_pages)}"
        )

    toc_doc = ChessWorkbookDoc(str(TOC_PDF), mirror_margin=True, mode="toc")
    toc_story, toc = build_toc_story(styles, chapter_pages)
    toc._lastEntries = list(toc._entries)
    toc_doc.build(toc_story)

    return CONTENT_PDF, TOC_PDF


def main() -> None:
    parser = argparse.ArgumentParser(description="Build chess workbook PDF")
    parser.add_argument(
        "--single-margin",
        action="store_true",
        help="Build one PDF (TOC + content) with centered margins",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print debug information while building",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if not ROOT_DIR.is_dir():
        raise SystemExit(f"Chapter directory not found: {ROOT_DIR}")

    register_fonts()

    if args.single_margin:
        combined_pdf = build_combined_pdf()
        print(f"Created: {combined_pdf}")
    else:
        content_pdf, toc_pdf = build_split_pdfs()
        print(f"Created: {content_pdf}")
        print(f"Created: {toc_pdf}")


if __name__ == "__main__":
    main()
