import argparse
import logging
import re
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ocr_utils import (
    normalize_text,
    ocr_description_region,
    ocr_image,
    require_tesseract,
)
from translate_io import format_translate_file
from workbook_io import (
    RAW_DIR,
    ROOT_DIR,
    list_images,
    next_chapter_number,
    prepare_item_dir,
    slugify_chapter_name,
)

START_KEYWORDS = ("bat dau", "bat đau")
CONTINUE_KEYWORDS = ("tiep theo", "tiep tuc")
TURN_LINE_PATTERN = re.compile(r"den\s+luot", re.IGNORECASE)
STATUS_BAR_PATTERN = re.compile(r"\d{1,2}:\d{2}")
CHALLENGE_PATTERN = re.compile(r"thu\s*thach", re.IGNORECASE)
FEEDBACK_PATTERN = re.compile(r"^\s*@", re.IGNORECASE)
CHESS_MOVE_PATTERN = re.compile(
    r"(?:[NBRQK][a-h]?x?[a-h][1-8]|[a-h]x[a-h][1-8])",
    re.IGNORECASE,
)
CORRECT_ANSWER_PATTERN = re.compile(r"\bdun\w*\b", re.IGNORECASE)
CONGRATULATION_PATTERN = re.compile(r"congratulation!?", re.IGNORECASE)
OCR_FRAGMENT_PATTERN = re.compile(r"^\d+\s+[a-z]{2,5}$", re.IGNORECASE)
HINT_PATTERN = re.compile(r"^gợi\s+y|goi\s+y|^\s*ii\s*:?\s*$", re.IGNORECASE)
OCR_JUNK_PATTERN = re.compile(r"^L[\}\)\{]|^[\^~`]+")


class ScreenKind(Enum):
    START = "start"
    CONTINUE = "continue"
    CONTENT = "content"


@dataclass
class ParsedScreen:
    kind: ScreenKind
    header: str = ""
    description: str = ""
    action: str = ""


@dataclass
class PrepareResult:
    chapter_dir: Path | None = None
    group_name: str = ""
    chapter_description: str = ""
    translate_blocks: list[list[str]] = field(default_factory=list)
    item_image_counts: dict[str, int] = field(default_factory=dict)


class PrepareSession:
    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run
        self.moves: list[tuple[Path, Path]] = []
        self.chapter_dir: Path | None = None
        self.chapter_created = False
        self.start_image: Path | None = None
        self.initial_images: list[Path] = []
        self.rolled_back = False

    def create_chapter(self, header: str) -> Path:
        order = next_chapter_number()
        slug = slugify_chapter_name(header)
        label = f"{order:02d}_{slug}"
        chapter_dir = ROOT_DIR / label

        if self.dry_run:
            self.chapter_dir = chapter_dir
            logging.info("  would create chapter: %s", label)
            return chapter_dir

        if chapter_dir.exists():
            raise FileExistsError(f"Chapter already exists: {chapter_dir}")

        chapter_dir.mkdir(parents=True)
        self.chapter_dir = chapter_dir
        self.chapter_created = True
        logging.info("  created chapter: %s", chapter_dir.name)
        return chapter_dir

    def move(self, source: Path, dest: Path, *, log_prefix: str) -> None:
        logging.info("  move %s -> %s", source.name, log_prefix)
        if self.dry_run:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        self.moves.append((source, dest))

    def rollback(self) -> None:
        if self.dry_run or self.rolled_back:
            return

        rollback_errors: list[str] = []
        for source, dest in reversed(self.moves):
            try:
                if dest.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(source))
            except OSError as exc:
                rollback_errors.append(f"{dest.name} -> {source.name}: {exc}")

        if self.chapter_created and self.chapter_dir and self.chapter_dir.exists():
            try:
                shutil.rmtree(self.chapter_dir)
            except OSError as exc:
                rollback_errors.append(f"remove {self.chapter_dir.name}: {exc}")

        self.rolled_back = True
        self.moves.clear()
        self.chapter_dir = None
        self.chapter_created = False

        if rollback_errors:
            raise RuntimeError(
                "Rollback incomplete:\n  " + "\n  ".join(rollback_errors)
            )

    def finalize_start_image(self) -> None:
        if self.dry_run or self.start_image is None:
            return

        moved_sources = {source for source, _ in self.moves}
        for image in self.initial_images:
            if image == self.start_image:
                continue
            if image in moved_sources:
                continue
            if image.exists():
                raise RuntimeError(
                    f"Not all images were moved; {image.name} still in raw/"
                )

        if not self.start_image.exists():
            return

        remaining = list_images(RAW_DIR)
        unexpected = [path for path in remaining if path != self.start_image]
        if unexpected:
            names = ", ".join(path.name for path in unexpected)
            raise RuntimeError(
                f"Cannot delete start image; unexpected files in raw/: {names}"
            )

        self.start_image.unlink()
        logging.info("Deleted start image: %s", self.start_image.name)


def meaningful_lines(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return lines


def is_turn_line(line: str) -> bool:
    return bool(TURN_LINE_PATTERN.search(normalize_text(line)))


def is_action_line(line: str) -> str | None:
    normalized = normalize_text(line)
    if any(keyword in normalized for keyword in START_KEYWORDS):
        return "bắt đầu"
    if "tiep theo" in normalized:
        return "tiếp theo"
    if "tiep tuc" in normalized:
        return "tiếp tục"
    return None


def is_status_bar_line(line: str) -> bool:
    return bool(STATUS_BAR_PATTERN.search(line))


def is_feedback_line(line: str) -> bool:
    normalized = normalize_text(line.replace("đ", "d").replace("Đ", "D"))
    if "@" in line and ("dung" in normalized or CORRECT_ANSWER_PATTERN.search(normalized)):
        return True
    if CHESS_MOVE_PATTERN.search(line) and (
        "dung" in normalized or CORRECT_ANSWER_PATTERN.search(normalized)
    ):
        return True
    return bool(FEEDBACK_PATTERN.match(line) and "dung" in normalized)


def is_ocr_fragment_line(line: str) -> bool:
    return bool(OCR_FRAGMENT_PATTERN.match(line.strip()))


def is_board_ocr_junk_line(line: str) -> bool:
    if "|" not in line or len(line) >= 40:
        return False
    words = re.findall(r"[A-Za-z]{3,}", line)
    return len(words) <= 2


def is_challenge_line(line: str) -> bool:
    return bool(CHALLENGE_PATTERN.search(normalize_text(line)))


def is_hint_or_junk_line(line: str) -> bool:
    if len(line.strip()) <= 2:
        return True
    if OCR_JUNK_PATTERN.match(line):
        return True
    return bool(HINT_PATTERN.search(normalize_text(line)))


def is_chapter_breadcrumb(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("€") and not stripped.startswith("@"):
        return False
    if is_feedback_line(line):
        return False
    text = stripped.lstrip("€@ ").strip()
    return "." not in text and len(text) < 40


def clean_chapter_header(line: str) -> str:
    text = line.lstrip("€@ ").strip()
    text = re.sub(r"\s*[>»].*$", "", text)
    text = re.sub(r"\s+Beg\s*$", "", text, flags=re.IGNORECASE)
    # Icons/badges beside the chapter name often OCR as {, %, digits, etc.
    text = re.sub(r"\s+(?:[\{|\}%\d].*)$", "", text).strip()
    return text


def extract_chapter_header(lines: list[str]) -> str:
    for line in lines:
        if line.lstrip().startswith("€"):
            return clean_chapter_header(line)
    return clean_chapter_header(pick_chapter_header(lines))


def should_skip_line(line: str) -> bool:
    if is_status_bar_line(line):
        return True
    if is_turn_line(line):
        return True
    if is_action_line(line):
        return True
    if is_feedback_line(line):
        return True
    if is_ocr_fragment_line(line):
        return True
    if is_board_ocr_junk_line(line):
        return True
    if is_challenge_line(line):
        return True
    if is_hint_or_junk_line(line):
        return True
    if is_chapter_breadcrumb(line):
        return True
    return False


def to_single_line(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def clean_description_line(line: str) -> str:
    match = CONGRATULATION_PATTERN.search(line)
    if match:
        return line[match.start() :].strip()
    return line


def merge_descriptions(desc_full: str, desc_top: str) -> str:
    if not desc_full:
        return desc_top
    if not desc_top:
        return desc_full

    normalized_full = re.sub(r"[^\w\s]", "", normalize_text(desc_full))
    normalized_top = re.sub(r"[^\w\s]", "", normalize_text(desc_top))
    if normalized_top.startswith(normalized_full) and len(normalized_top) > len(
        normalized_full
    ):
        extra_words = normalized_top[len(normalized_full) :].strip().split()
        if len(extra_words) <= 6:
            return desc_top
    return desc_full


def extract_description_text(lines: list[str]) -> str:
    parts = []
    for line in lines:
        if should_skip_line(line):
            continue
        parts.append(clean_description_line(line))
    return to_single_line(parts)


def pick_chapter_header(lines: list[str]) -> str:
    for line in lines:
        if is_status_bar_line(line):
            continue
        if is_action_line(line):
            continue
        if is_turn_line(line):
            continue
        if line.startswith("€") or line.startswith("@"):
            return line.lstrip("€@ ").strip()
        if len(line) > 2:
            return line
    return lines[0] if lines else ""


def classify_screen(text: str) -> ScreenKind:
    normalized = normalize_text(text)
    if any(keyword in normalized for keyword in START_KEYWORDS):
        return ScreenKind.START
    if any(keyword in normalized for keyword in CONTINUE_KEYWORDS):
        return ScreenKind.CONTINUE
    return ScreenKind.CONTENT


def parse_screen(
    ocr_text: str,
    *,
    description_ocr_text: str | None = None,
) -> ParsedScreen:
    kind = classify_screen(ocr_text)
    lines = meaningful_lines(ocr_text)
    if not lines:
        logging.warning("Empty OCR text; treating as content")
        return ParsedScreen(kind=ScreenKind.CONTENT)

    header = extract_chapter_header(lines) if kind == ScreenKind.START else ""
    description = extract_description_text(lines)
    if description_ocr_text and kind != ScreenKind.START:
        desc_top = extract_description_text(meaningful_lines(description_ocr_text))
        description = merge_descriptions(description, desc_top)
    action = ""
    for line in lines:
        if action := is_action_line(line):
            break

    return ParsedScreen(
        kind=kind,
        header=header,
        description=description,
        action=action,
    )


def move_item_images(
    session: PrepareSession,
    item_number: int,
    images: list[Path],
) -> None:
    if not images:
        return

    if session.dry_run:
        if session.chapter_dir is None:
            raise RuntimeError("Chapter directory not created")
        chapter_label = session.chapter_dir.name
        item_name = f"item{item_number:03d}"
        for index, source in enumerate(images, start=1):
            dest_name = f"{index:03d}{source.suffix.lower()}"
            logging.info(
                "  move %s -> %s/%s/raw/%s",
                source.name,
                chapter_label,
                item_name,
                dest_name,
            )
        return

    if session.chapter_dir is None:
        raise RuntimeError("Chapter directory not created")

    item_dir = prepare_item_dir(session.chapter_dir, item_number)
    for index, source in enumerate(images, start=1):
        dest_name = f"{index:03d}{source.suffix.lower()}"
        dest = item_dir / "raw" / dest_name
        session.move(
            source,
            dest,
            log_prefix=f"{session.chapter_dir.name}/{item_dir.name}/raw/{dest_name}",
        )


def process_raw(*, dry_run: bool = False) -> PrepareResult:
    raw_dir = RAW_DIR
    if not raw_dir.is_dir():
        raise SystemExit(f"Raw directory not found: {raw_dir}")

    images = list_images(raw_dir)
    if not images:
        raise SystemExit(f"No images found in {raw_dir}")

    session = PrepareSession(dry_run=dry_run)
    session.initial_images = images

    result = PrepareResult()
    current_item = 0
    pending_images: list[Path] = []
    current_item_descriptions: list[str] = []
    translate_blocks: list[list[str]] = []
    saw_start = False

    try:
        for image_path in images:
            if not image_path.exists():
                logging.warning("Skipping missing file: %s", image_path.name)
                continue

            logging.info("Scanning %s", image_path.name)
            ocr_text = ocr_image(image_path)
            description_ocr_text = ocr_description_region(image_path)
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.debug("OCR for %s:\n%s", image_path.name, ocr_text)
                logging.debug(
                    "Description OCR for %s:\n%s",
                    image_path.name,
                    description_ocr_text,
                )

            parsed = parse_screen(
                ocr_text,
                description_ocr_text=description_ocr_text,
            )
            logging.info(
                "  -> %s header=%r description=%r",
                parsed.kind.value,
                parsed.header[:40] if parsed.header else "",
                parsed.description[:80] if parsed.description else "",
            )

            if parsed.kind == ScreenKind.START:
                if saw_start:
                    logging.warning(
                        "Multiple 'bắt đầu' screens found; ignoring %s",
                        image_path.name,
                    )
                    continue

                saw_start = True
                session.start_image = image_path
                result.group_name = parsed.header
                result.chapter_description = parsed.description

                chapter_dir = session.create_chapter(parsed.header)
                result.chapter_dir = session.chapter_dir or chapter_dir
                current_item = 1
                if not dry_run:
                    prepare_item_dir(session.chapter_dir, current_item)
                logging.info(
                    "  chapter metadata -> translate.txt (group/description empty)"
                )
                continue

            if not saw_start:
                logging.warning(
                    "Skipping %s: no 'bắt đầu' screen seen yet",
                    image_path.name,
                )
                continue

            if parsed.description:
                current_item_descriptions.append(parsed.description)

            if parsed.kind == ScreenKind.CONTINUE:
                pending_images.append(image_path)
                move_item_images(session, current_item, pending_images)
                result.item_image_counts[f"item{current_item:03d}"] = len(
                    pending_images
                )
                if current_item_descriptions:
                    translate_blocks.append(current_item_descriptions)
                current_item_descriptions = []
                pending_images = []
                current_item += 1
                continue

            pending_images.append(image_path)

        if not saw_start:
            raise RuntimeError(
                f"No 'bắt đầu' screen found in {raw_dir}. Cannot prepare chapter."
            )

        if pending_images:
            move_item_images(session, current_item, pending_images)
            result.item_image_counts[f"item{current_item:03d}"] = len(pending_images)
            if current_item_descriptions:
                translate_blocks.append(current_item_descriptions)

        session.finalize_start_image()

        result.translate_blocks = translate_blocks
        if not dry_run and session.chapter_dir is not None:
            (session.chapter_dir / "group.txt").write_text("", encoding="utf-8")
            (session.chapter_dir / "description.txt").write_text("", encoding="utf-8")
            translate_body = format_translate_file(
                result.group_name,
                result.chapter_description,
                translate_blocks,
            )
            (session.chapter_dir / "translate.txt").write_text(
                translate_body,
                encoding="utf-8",
            )

    except Exception:
        if not dry_run:
            try:
                session.rollback()
                logging.error("Rolled back all moves and removed created chapter.")
            except RuntimeError as rollback_exc:
                logging.error("Rollback failed: %s", rollback_exc)
                raise RuntimeError(
                    "Prepare failed and rollback was incomplete"
                ) from rollback_exc
        raise

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare chapter from img_root/raw/ screenshots via OCR"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print classification and move plan without writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print OCR text for each image",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    require_tesseract()
    print(f"Scanning: {RAW_DIR}")

    try:
        result = process_raw(dry_run=args.dry_run)
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print()
    if result.chapter_dir:
        print(f"Chapter: {result.chapter_dir.name}")
    elif args.dry_run:
        print("Chapter: (dry-run, not created)")
    print(f"Group: {result.group_name!r}")
    print(f"Description: {len(result.chapter_description)} chars")
    print(f"Items: {len(result.item_image_counts)}")
    for item_name, count in result.item_image_counts.items():
        print(f"  {item_name}: {count} images")
    print(f"Translate blocks: {len(result.translate_blocks)}")
    for index, block in enumerate(result.translate_blocks, start=1):
        print(f"  item{index:03d}: {len(block)} description(s)")
    if args.dry_run:
        print("(dry-run: no files written or moved)")
    elif result.chapter_dir:
        print(f"Wrote: {result.chapter_dir / 'group.txt'} (empty)")
        print(f"Wrote: {result.chapter_dir / 'description.txt'} (empty)")
        print(f"Wrote: {result.chapter_dir / 'translate.txt'}")
        suffix = result.chapter_dir.name.split("_", 1)[-1]
        print(f"Paste translations below '---' then run: python apply_translate.py {suffix}")


if __name__ == "__main__":
    main()
