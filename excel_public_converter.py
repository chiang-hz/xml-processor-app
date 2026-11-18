"""Excel 資產負債表公開版轉換器：僅發布公開工作表，不夾帶原始明細。

Dependency: openpyxl.  No Streamlit dependency in this module.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import copy, deepcopy
from dataclasses import dataclass, field
from io import BytesIO
from math import fsum
from typing import Optional
import posixpath
import re
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from excel_print_pagination import read_excel_page_ends

TARGET_PARENTS = ('遞延資產', '負債準備')
PUBLIC_SUFFIX = '-公開上網版'
MAIN = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL_TYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = f'{{{REL_TYPE}}}'
PACKAGE = '{http://schemas.openxmlformats.org/package/2006/relationships}'
CONTENT_TYPE = '{http://schemas.openxmlformats.org/package/2006/content-types}'
DRAWING = '{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}'
ART = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
MC = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
CAMERA = '{http://schemas.microsoft.com/office/drawing/2010/main}cameraTool'
NOTES_RANGE = re.compile(r"(?:\[\d+\])?(?:附註說明|'附註說明')!\$?[A-Z]+\$?[1-9]\d*:\$?[A-Z]+\$?[1-9]\d*")


@dataclass
class ConversionSummary:
    source_sheet: str
    output_sheet: str
    removed: list[dict] = field(default_factory=list)
    filled_zero_count: int = 0
    reference_mismatch_count: Optional[int] = None
    reference_mismatches: list[str] = field(default_factory=list)
    applied_reference_heights: bool = False
    warning_messages: list[str] = field(default_factory=list)
    original_last_row: int = 0
    output_last_row: int = 0
    drawing_preserved: bool = False
    footer_notes_preserved: bool = False
    printer_setting_preserved: bool = False
    adjusted_print_pages: int = 0


def _norm_label(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def _indent(value):
    if value is None:
        return -1
    s = str(value)
    match = re.match(r'^[\u3000 \t]*', s)
    prefix = match.group(0) if match else ''
    # The source format uses one ideographic space per level.
    return prefix.count('\u3000') + (len(prefix) - prefix.count('\u3000')) // 2


def _find_child_deletions(sheet):
    matches = set()
    found_parents = set()
    active_parent = None
    active_level = -1
    records = []
    for row_idx in range(1, sheet.max_row + 1):
        raw = sheet.cell(row_idx, 1).value
        if raw is None or not str(raw).strip():
            continue
        label = _norm_label(raw)
        level = _indent(raw)
        if active_parent is not None and level <= active_level:
            active_parent = None
        if label in TARGET_PARENTS and level >= 1:
            found_parents.add(label)
            active_parent, active_level = label, level
            continue
        if active_parent is not None and level > active_level:
            matches.add(row_idx)
            records.append(dict(parent=active_parent, original_row=row_idx,
                                label=str(raw).strip(), values=[sheet.cell(row_idx, c).value for c in (2, 3, 4, 5)]))
    if not found_parents:
        raise ValueError('未找到指定父科目「遞延資產」或「負債準備」，為避免誤製公開檔，已停止。')
    for parent in found_parents:
        if not any(x['parent'] == parent for x in records):
            # This is acceptable if the source has already been converted.
            pass
    return matches, records, found_parents


def _remap_merged_ranges(src, dst, old_to_new):
    """Preserve horizontal merges after row deletion without shifting content into merges.

    Vertical merges are retained only if every original row remains present and
    maps to a contiguous sequence. Partial deletion is rejected explicitly.
    """
    planned = []
    for merged in src.merged_cells.ranges:
        rows = list(range(merged.min_row, merged.max_row + 1))
        kept = [old_to_new[r] for r in rows if r in old_to_new]
        if not kept:
            # Entire merged region is in deleted rows.
            continue
        if len(kept) != len(rows) or kept != list(range(kept[0], kept[0] + len(rows))):
            raise ValueError(f'合併範圍 {merged} 橫跨被刪除的列，無法安全保留；請先調整合併範圍。')
        planned.append(f'{get_column_letter(merged.min_col)}{kept[0]}:'
                       f'{get_column_letter(merged.max_col)}{kept[-1]}')
    # copy_worksheet currently does not replicate merges, but clear in case
    # a future openpyxl version changes that behavior.
    dst.merged_cells.ranges.clear()
    for addr in planned:
        dst.merge_cells(addr)
    return planned


def _apply_row_mapping(src, dst, old_to_new, deleted_rows, preserve_layout):
    cells = {}
    for (row, col), cell in dst._cells.items():
        if row not in old_to_new:
            continue
        row = old_to_new[row]
        cell.row = row
        cell.comment = None
        cell.hyperlink = None
        cells[row, col] = cell
    dst._cells = cells
    dst.row_dimensions.clear()
    original_last_row = len(old_to_new) + len(deleted_rows)
    for row, dim in src.row_dimensions.items():
        if row in old_to_new or row > original_last_row:
            new_row = row - bisect_left(deleted_rows, row)
            moved = copy(dim)
            moved.index = new_row
            moved.worksheet = dst
            dst.row_dimensions[new_row] = moved

    print_areas = []
    for area in src._print_area.sorted():
        first = area.min_row - bisect_left(deleted_rows, area.min_row)
        last = area.max_row - bisect_right(deleted_rows, area.max_row)
        if first <= last:
            print_areas.append(f'{get_column_letter(area.min_col)}{first}:{get_column_letter(area.max_col)}{last}')
    if preserve_layout and not src.print_area:
        dst.print_area = f'A1:{get_column_letter(src.max_column)}{len(old_to_new)}'
    else:
        dst.print_area = print_areas


def _print_sections(src, row_count, page_ends=None):
    boundaries = {0, row_count}
    if page_ends is None:
        page_ends = [br.id for br in src.row_breaks.brk]
    boundaries.update(row for row in page_ends if 0 < row < row_count)
    for area in src._print_area.ranges:
        boundaries.update(i for i in (area.min_row - 1, area.max_row) if 0 < i < row_count)
    boundaries = sorted(boundaries)
    return zip(boundaries, boundaries[1:])


def _printable_row_height(sheet, row):
    dimension = sheet.row_dimensions.get(row)
    if dimension is None:
        return 0 if sheet.sheet_format.zeroHeight else sheet.sheet_format.defaultRowHeight or 15
    if dimension.hidden:
        return 0
    return dimension.height if dimension.height is not None else sheet.sheet_format.defaultRowHeight or 15


def _add_row_height(src, out, source_row, output_row, increase):
    height = round(_printable_row_height(src, source_row) + increase, 8)
    if height > 409:
        raise ValueError('分攤後的列高超過 Excel 可容納的上限，請取消「保留原始分頁」後重試。')
    out.row_dimensions[output_row].height = height


def _distribute_page_heights(src, out, old_to_new, deleted_rows, page_ends):
    row_count = len(old_to_new) + len(deleted_rows)
    title = src._print_rows
    explicit_areas = src._print_area.ranges
    adjusted_pages = 0
    for begin, end in _print_sections(src, row_count, page_ends):
        if explicit_areas and not any(area.min_row <= begin + 1 <= area.max_row for area in explicit_areas):
            continue
        removed_height = fsum(_printable_row_height(src, row)
                              for row in deleted_rows[bisect_right(deleted_rows, begin):bisect_right(deleted_rows, end)])
        if not removed_height:
            continue
        accounts = [row for row in range(max(6, begin + 1), end + 1)
                    if row in old_to_new and _printable_row_height(src, row) > 0
                    and (title is None or not title.min_row <= row <= title.max_row)
                    and _norm_label(src.cell(row, 1).value)]
        if not accounts:
            raise ValueError('刪除明細後，同一頁沒有可分攤列高的科目列，請取消「保留原始分頁」後重試。')
        increase = removed_height / len(accounts)
        for row in accounts:
            _add_row_height(src, out, row, old_to_new[row], increase)
        adjusted_pages += 1
    return adjusted_pages


def _absorb_with_adjacent_rows(src, out, old_to_new):
    """Keep the previous layout behavior when Excel's automatic breaks are unavailable."""
    extra_heights = {}
    for begin, end in _print_sections(src, src.max_row):
        previous = None
        leading_height = 0
        for row in range(begin + 1, end + 1):
            height = _printable_row_height(src, row)
            if not height:
                continue
            if row in old_to_new:
                previous = row
                if leading_height:
                    extra_heights[previous] = leading_height
                    leading_height = 0
            elif previous is None:
                leading_height += height
            else:
                extra_heights[previous] = extra_heights.get(previous, 0) + height
        if leading_height:
            raise ValueError('一段列印範圍內沒有可承接刪除列高度的科目，請取消「保留原始分頁」後重試。')

    for row, extra in extra_heights.items():
        _add_row_height(src, out, row, old_to_new[row], extra)


def _keep_excel_page_breaks(out, page_ends, deleted_rows, output_last_row):
    page_setup = out.sheet_properties.pageSetUpPr
    if (page_setup is not None and page_setup.fitToPage) or len(out._print_area.ranges) > 1:
        return
    for end in page_ends:
        output_end = end - bisect_right(deleted_rows, end)
        if 0 < output_end < output_last_row and all(br.id != output_end for br in out.row_breaks.brk):
            out.row_breaks.append(Break(id=output_end))
    out.row_breaks.brk.sort(key=lambda br: br.id)


def _restore_page_parameters(src, out, deleted_rows):
    for attr in ('page_setup', 'page_margins', 'print_options', 'sheet_properties',
                 'sheet_format', 'HeaderFooter', 'views'):
        setattr(out, attr, deepcopy(getattr(src, attr)))
    out.print_title_rows = src.print_title_rows
    out.print_title_cols = src.print_title_cols
    out.row_breaks = deepcopy(src.row_breaks)
    for br in out.row_breaks.brk:
        br.id -= bisect_right(deleted_rows, br.id)
    out.col_breaks = deepcopy(src.col_breaks)
    out.sheet_state = 'visible'


def _validate_against_reference(out, ref):
    problems = []
    max_r = max(out.max_row, ref.max_row)
    for r in range(1, max_r + 1):
        for c in range(1, 6):
            v1 = out.cell(r, c).value
            v2 = ref.cell(r, c).value
            if v1 != v2:
                problems.append(f'{get_column_letter(c)}{r}: 產生值={str(v1)[:40]!r}, 參考值={str(v2)[:40]!r}')
    return problems


def _copy_reference_heights(out, ref):
    for r in range(1, out.max_row + 1):
        if r in ref.row_dimensions:
            out.row_dimensions[r].height = ref.row_dimensions[r].height


def _relationships_path(path):
    directory, name = posixpath.split(path)
    return posixpath.join(directory, '_rels', name + '.rels')


def _part_relationships(source, path):
    try:
        root = ET.fromstring(source.read(_relationships_path(path)))
    except KeyError:
        return {}
    return {item.get('Id'): item for item in root.findall(f'{PACKAGE}Relationship')}


def _related_part(source, parent, relationships, rel_id, kind, folder):
    relationship = relationships.get(rel_id)
    if (relationship is None or relationship.get('Type') != f'{REL_TYPE}/{kind}'
            or relationship.get('TargetMode', 'Internal') != 'Internal'):
        return None
    target = relationship.get('Target', '')
    if not target or any(char in target for char in ':\\?#'):
        return None
    if target.startswith('/'):
        target = target.lstrip('/')
    else:
        target = posixpath.join(posixpath.dirname(parent), target)
    target = posixpath.normpath(target)
    if target.startswith(folder + '/') and target in source.namelist():
        return target
    return None


def _drawing_row_markers(anchor):
    markers = []
    for corner in ('from', 'to'):
        marker = anchor.find(f'{DRAWING}{corner}/{DRAWING}row')
        if marker is not None:
            markers.append(marker)
    return markers


def _public_drawing(source, path, deleted_rows, last_account_row, summary):
    """Keep self-contained header text and the printed notes camera's static cache."""
    root = ET.fromstring(source.read(path))
    relationships = _part_relationships(source, path)
    drawing = ET.Element(f'{DRAWING}wsDr')
    images = {}
    for item in root:
        anchor = item
        if item.tag == f'{MC}AlternateContent':
            anchor = item.find(f'{MC}Choice/{DRAWING}twoCellAnchor')
        if anchor is None or anchor.tag not in (f'{DRAWING}oneCellAnchor', f'{DRAWING}twoCellAnchor'):
            continue
        markers = _drawing_row_markers(anchor)
        if not markers or any(m.text is None or not m.text.isdecimal() for m in markers):
            continue
        rows = [int(m.text) + 1 for m in markers]
        if set(rows).intersection(deleted_rows):
            continue

        shape = anchor.find(f'{DRAWING}sp')
        camera = anchor.find(f'.//{CAMERA}')
        image = None
        blip = None
        if shape is not None and max(rows) <= 5 and shape.find(f'{DRAWING}txBody') is not None:
            if shape.get('macro') or shape.get('textlink'):
                continue
        elif camera is not None and rows[0] > last_account_row and NOTES_RANGE.fullmatch(camera.get('cellRange', '')):
            blip = anchor.find(f'{DRAWING}pic/{DRAWING}blipFill/{ART}blip')
            if blip is None:
                continue
            image_path = _related_part(source, path, relationships, blip.get(f'{REL}embed'), 'image', 'xl/media')
            if image_path is None or not image_path.lower().endswith('.emf'):
                continue
            image = source.read(image_path)
            if image[40:44] != b' EMF':
                continue
        else:
            continue

        # Drop the Camera binding and compatibility extensions. Only the
        # visible snapshot and its native position survive in the public sheet.
        for element in list(anchor.iter()):
            for child in list(element):
                if child.tag in (f'{ART}extLst', f'{DRAWING}extLst'):
                    element.remove(child)
        if any(key.startswith(REL) and (element is not blip or key != f'{REL}embed')
               for element in anchor.iter() for key in element.attrib):
            continue
        for marker, row in zip(markers, rows):
            marker.text = str(row - bisect_left(deleted_rows, row) - 1)
        drawing.append(anchor)
        if image is not None:
            image_id = f'rId{len(images) + 1}'
            blip.set(f'{REL}embed', image_id)
            images[image_id] = image
            summary.footer_notes_preserved = True
        else:
            summary.drawing_preserved = True
    return drawing, images


def _restore_print_assets(source_bytes, output_bytes, deleted_rows, last_account_row, summary):
    drawing, images, printer = None, {}, None
    with ZipFile(BytesIO(source_bytes)) as source:
        workbook_path = 'xl/workbook.xml'
        workbook = ET.fromstring(source.read(workbook_path))
        sheets = workbook.findall(f'{MAIN}sheets/{MAIN}sheet')
        source_sheet = next(item for item in sheets if item.get('name') == summary.source_sheet)
        sheet_path = _related_part(source, workbook_path, _part_relationships(source, workbook_path),
                                   source_sheet.get(f'{REL}id'), 'worksheet', 'xl/worksheets')
        if sheet_path is None:
            return output_bytes
        original = ET.fromstring(source.read(sheet_path))
        relationships = _part_relationships(source, sheet_path)
        item = original.find(f'{MAIN}drawing')
        if item is not None:
            path = _related_part(source, sheet_path, relationships, item.get(f'{REL}id'), 'drawing', 'xl/drawings')
            if path is not None:
                drawing, images = _public_drawing(source, path, deleted_rows, last_account_row, summary)
        item = original.find(f'{MAIN}pageSetup')
        if item is not None:
            path = _related_part(source, sheet_path, relationships, item.get(f'{REL}id'), 'printerSettings',
                                 'xl/printerSettings')
            if path is not None:
                printer = source.read(path)
    has_drawing = drawing is not None and len(drawing) > 0
    if not has_drawing and printer is None:
        return output_bytes

    with ZipFile(BytesIO(output_bytes)) as prior:
        archive = {name: prior.read(name) for name in prior.namelist()}
    sheet_path = 'xl/worksheets/sheet1.xml'
    sheet = ET.fromstring(archive[sheet_path])
    rel_path = _relationships_path(sheet_path)
    rel_root = ET.fromstring(archive[rel_path]) if rel_path in archive else ET.Element(f'{PACKAGE}Relationships')
    content_types = ET.fromstring(archive['[Content_Types].xml'])

    def add_part(path, content, kind, content_type, parent, rels, rel_id=None):
        if rel_id is None:
            taken = {item.get('Id') for item in rels}
            index = 1
            while f'rId{index}' in taken:
                index += 1
            rel_id = f'rId{index}'
        archive[path] = content
        ET.SubElement(content_types, f'{CONTENT_TYPE}Override', PartName='/' + path, ContentType=content_type)
        ET.SubElement(rels, f'{PACKAGE}Relationship',
                      Id=rel_id, Type=f'{REL_TYPE}/{kind}',
                      Target=posixpath.relpath(path, posixpath.dirname(parent)))
        return rel_id

    if printer is not None:
        rel_id = add_part('xl/printerSettings/public_printer.bin', printer, 'printerSettings',
                          'application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings',
                          sheet_path, rel_root)
        page_setup = sheet.find(f'{MAIN}pageSetup')
        if page_setup is None:
            page_setup = ET.Element(f'{MAIN}pageSetup')
            sheet.insert(list(sheet).index(sheet.find(f'{MAIN}pageMargins')) + 1, page_setup)
        page_setup.set(f'{REL}id', rel_id)
        summary.printer_setting_preserved = True
    if has_drawing:
        drawing_path = 'xl/drawings/public_print.xml'
        rel_id = add_part(drawing_path, ET.tostring(drawing), 'drawing',
                          'application/vnd.openxmlformats-officedocument.drawing+xml', sheet_path, rel_root)
        ET.SubElement(sheet, f'{MAIN}drawing', {f'{REL}id': rel_id})
        if images:
            drawing_rels = ET.Element(f'{PACKAGE}Relationships')
            for index, (image_id, image) in enumerate(images.items(), 1):
                add_part(f'xl/media/public_notes{index}.emf', image, 'image', 'image/x-emf',
                         drawing_path, drawing_rels, image_id)
            archive[_relationships_path(drawing_path)] = ET.tostring(drawing_rels)

    archive[sheet_path] = ET.tostring(sheet, encoding='utf-8', xml_declaration=True)
    archive[rel_path] = ET.tostring(rel_root, encoding='utf-8', xml_declaration=True)
    archive['[Content_Types].xml'] = ET.tostring(content_types, encoding='utf-8', xml_declaration=True)
    buff = BytesIO()
    with ZipFile(buff, 'w', ZIP_DEFLATED) as z:
        for name, content in archive.items():
            z.writestr(name, content)
    return buff.getvalue()


def convert_excel_public(source_bytes, source_sheet=None, reference_sheet=None,
                         fill_zero=False, preserve_layout=True):
    """Return a public-only .xlsx and a detailed ConversionSummary.

    The public workbook contains NO source, reference or deleted-rows audit sheet.
    Input source_bytes is never modified.
    """
    workbook = load_workbook(BytesIO(source_bytes), data_only=False, keep_links=False)
    if not workbook.worksheets:
        raise ValueError('找不到工作表。')
    src = workbook[source_sheet] if source_sheet else workbook.worksheets[0]
    ref = workbook[reference_sheet] if reference_sheet and reference_sheet != src.title else None
    deletes, removed_records, found_parents = _find_child_deletions(src)
    if not deletes:
        raise ValueError('找到指定父科目，但沒有可刪除子科目。請確認使用的是完整原始版。')
    if len(deletes) < len(found_parents):
        raise ValueError('指定父科目子科目數量異常，已停止；請先檢查工作表。')
    if src._images or src._charts:
        raise ValueError('來源含圖片或圖表，現有版本無法安全保留其位置，請先移至單獨頁面再處理。')
    if any(cell.data_type == 'f' for row in src.iter_rows(min_row=6) for cell in row):
        raise ValueError('資料區含公式：為避免刪除列後的參照錯誤，請先核對並轉為值，再執行。')
    kept_rows = [row for row in range(1, src.max_row + 1) if row not in deletes]
    deleted_rows = sorted(deletes)
    old_to_new = {row: i for i, row in enumerate(kept_rows, 1)}
    output_last_row = len(kept_rows)
    summary = ConversionSummary(source_sheet=src.title, output_sheet='', removed=removed_records,
                                original_last_row=src.max_row, output_last_row=output_last_row)
    out = workbook.copy_worksheet(src)
    desired_name = src.title + PUBLIC_SUFFIX
    if len(desired_name) > 31:
        desired_name = '公開上網版'
    temp_name = '公開上網版_自動產生'
    i = 2
    while temp_name in workbook.sheetnames:
        temp_name = f'公開版_自動產生{i}'
        i += 1
    out.title = temp_name
    summary.output_sheet = desired_name
    _restore_page_parameters(src, out, deleted_rows)
    _apply_row_mapping(src, out, old_to_new, deleted_rows, preserve_layout)
    _remap_merged_ranges(src, out, old_to_new)
    if preserve_layout:
        page_ends = read_excel_page_ends(source_bytes, src.title)
        if page_ends is None:
            _absorb_with_adjacent_rows(src, out, old_to_new)
            summary.warning_messages.append('無法從桌面版 Excel 取得原始分頁，已由相鄰列承接刪除列的高度。')
        else:
            summary.adjusted_print_pages = _distribute_page_heights(src, out, old_to_new, deleted_rows, page_ends)
            if summary.adjusted_print_pages:
                _keep_excel_page_breaks(out, page_ends, deleted_rows, output_last_row)
    if fill_zero:
        for r in range(6, output_last_row + 1):
            if out.cell(r, 1).value is None:
                continue
            for c in (2, 3, 4, 5):
                if out.cell(r, c).value is None:
                    out.cell(r, c).value = 0
                    summary.filled_zero_count += 1
    if ref is not None:
        diffs = _validate_against_reference(out, ref)
        summary.reference_mismatch_count = len(diffs)
        summary.reference_mismatches = diffs[:30]
        if not preserve_layout:
            if diffs:
                summary.warning_messages.append('與參考工作表內容未完全一致，未沿用其列高。')
            else:
                _copy_reference_heights(out, ref)
                summary.applied_reference_heights = True
    # No raw/internal worksheet is ever published.
    for worksheet in list(workbook.worksheets):
        if worksheet is not out:
            workbook.remove(worksheet)
    workbook.active = 0
    out.title = desired_name
    workbook.properties.creator = ''
    workbook.properties.lastModifiedBy = ''
    workbook.properties.description = '資產負債表公開版，不含刪除之科目明細'
    dest = BytesIO()
    workbook.save(dest)
    last_account_row = max(row for (row, col), cell in src._cells.items() if col == 1 and cell.value is not None)
    data = _restore_print_assets(source_bytes, dest.getvalue(), deleted_rows, last_account_row, summary)
    # Ensure that no deleted account label remains in any reachable worksheet.
    check = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        if len(check.sheetnames) != 1:
            raise ValueError('公開版仍有非公開工作表，安全檢查未通過。')
        published_labels = {_norm_label(row[0]) for row in check.active.iter_rows(max_col=1, values_only=True)}
    finally:
        check.close()
    for item in removed_records:
        if _norm_label(item['label']) in published_labels:
            raise ValueError('公開版仍有應刪除科目，安全檢查未通過。')
    return data, summary
