# 導入需要的函式庫
import streamlit as st
import xml.etree.ElementTree as ET
from io import StringIO
import unicodedata


# XML 欄位別名集中設定：未來只需在對應欄位的 tuple 加入名稱，不必修改處理邏輯。
# 格式："程式內部欄位名稱": ("XML 標籤名稱1", "XML 標籤名稱2", ...)
XML_FIELD_ALIASES = {
    "account_id": (
        "科目編號",
        "科目-編號",
        # 若日後出現其他格式，例如 "科目代號"，直接在此新增即可。
    ),
}


def _normalize_xml_tag(tag):
    """對 XML 標籤做有限度的正規化（全形文字、常見連字號差異）。"""
    # 兼容保留命名空間的 Element；process_xml 亦會先移除命名空間。
    local_name = tag.rsplit("}", 1)[-1]
    normalized = unicodedata.normalize("NFKC", local_name).strip()
    for dash in ("‐", "‑", "‒", "–", "—", "−", "﹣"):
        normalized = normalized.replace(dash, "-")
    return normalized.casefold()


def find_field_element(row, field_key):
    """依別名順序查找 ROW 內的欄位；優先選取有內容的元素。

    即使主要別名的標籤存在但內容為空，也會嘗試其他別名。
    若所有同名欄位都為空，則回傳第一個找到的元素。
    """
    aliases = XML_FIELD_ALIASES[field_key]
    children = list(row)
    normalized_children = [(_normalize_xml_tag(child.tag), child) for child in children]

    for alias in aliases:
        normalized_alias = _normalize_xml_tag(alias)
        for child_name, child in normalized_children:
            if child_name == normalized_alias and (child.text or "").strip():
                return child

    for alias in aliases:
        normalized_alias = _normalize_xml_tag(alias)
        for child_name, child in normalized_children:
            if child_name == normalized_alias:
                return child

    return None


def process_xml(xml_string, enable_delete, enable_value_process):
    """
    處理 XML 字串。
    Args:
        xml_string (str): 原始 XML 內容。
        enable_delete (bool): 是否執行刪除指定科目。
        enable_value_process (bool): 是否執行數值翻轉邏輯。
    """
    try:
        xml_string = xml_string.strip()
        # 解析 XML 並處理命名空間
        it = ET.iterparse(StringIO(xml_string))
        for _, el in it:
            if '}' in el.tag:
                el.tag = el.tag.split('}', 1)[1]
        root = it.root

        dataset = root.find('DataSet')
        if dataset is None:
            st.error("錯誤：在檔案中找不到 <DataSet> 標籤。")
            return None, 0

        rows_to_remove = []
        
        for row in dataset.findall('ROW'):
            # --- 功能 A：刪除指定範圍 ---
            if enable_delete:
                account_id_el = find_field_element(row, "account_id")
                if account_id_el is not None and account_id_el.text:
                    try:
                        account_id = int(account_id_el.text)
                        if (190201 <= account_id <= 190299) or (280101 <= account_id <= 280199):
                            rows_to_remove.append(row)
                            continue 
                    except ValueError:
                        pass

            # --- 功能 B：數值處理 ---
            if enable_value_process:
                account_name_el = row.find('科目名稱')
                # 排除例外科目
                is_excluded = account_name_el is not None and "確定福利計畫之再衡量數" in (account_name_el.text or "")
                
                if not is_excluded:
                    triggered_flip = False
                    # 決算數處理 (負數轉正)
                    for tag in ['本年度決算數', '上年度決算數']:
                        el = row.find(tag)
                        if el is not None and el.text:
                            try:
                                val = float(el.text.replace(',', ''))
                                if val < 0:
                                    el.text = str(abs(val))
                                    triggered_flip = True
                            except ValueError:
                                pass
                    
                    # 增減金額處理 (條件式反轉)
                    if triggered_flip:
                        diff_el = row.find('比較增減-金額')
                        if diff_el is not None and diff_el.text:
                            try:
                                val = float(diff_el.text.replace(',', ''))
                                diff_el.text = str(val * -1)
                            except ValueError:
                                pass

        # 執行物理刪除
        for row in rows_to_remove:
            dataset.remove(row)

        # --- 自動化：序號重整 (SEQNO) ---
        remaining_rows = dataset.findall('ROW')
        if remaining_rows:
            try:
                first_seq_el = remaining_rows[0].find('SEQNO')
                current_seq = int(first_seq_el.text) if first_seq_el is not None else 1
            except (ValueError, TypeError):
                current_seq = 1
            
            for row in remaining_rows:
                seq_el = row.find('SEQNO')
                if seq_el is not None:
                    seq_el.text = str(current_seq)
                else:
                    new_seq = ET.SubElement(row, 'SEQNO')
                    new_seq.text = str(current_seq)
                current_seq += 1

        # 輸出處理後的 XML
        modified_xml_string = ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')
        return modified_xml_string, len(rows_to_remove)

    except Exception as e:
        st.error(f"處理失敗：{e}")
        return None, 0

# --- Streamlit 介面渲染 ---

def render_xml_tab():
    st.subheader('XML 科目處理')
    st.caption('保留原專案：科目編號範圍刪除、正負數處理及 SEQNO 重排。')
    left, right = st.columns(2)
    with left:
        do_delete = st.toggle('啟用「科目範圍刪除」', value=True,
                              help='科目編號 190201–190299 及 280101–280199')
        st.caption('可辨識欄位：' + '、'.join(XML_FIELD_ALIASES['account_id']))
    with right:
        do_value_adj = st.toggle('啟用「數值符號調整」', value=True)
        st.caption('負數決算數轉正，以及比較增減金額的條件式反轉。')
    uploaded_file = st.file_uploader('選擇 XML 檔案', type=['xml'], key='xml_input')
    if uploaded_file is None:
        return
    if st.button('開始 XML 處理', use_container_width=True, key='run_xml'):
        try:
            modified_xml, removed_count = process_xml(uploaded_file.getvalue().decode('utf-8-sig'),
                                                      do_delete, do_value_adj)
        except UnicodeDecodeError:
            st.error('XML 檔案文字編碼錯誤，請使用 UTF-8。')
            return
        if modified_xml:
            st.session_state['xml_result'] = (modified_xml, removed_count, uploaded_file.name)
    result = st.session_state.get('xml_result')
    if result and result[2] == uploaded_file.name:
        modified_xml, removed_count, original_name = result
        st.success(f'完成處理，刪除 {removed_count} 筆 XML ROW。')
        st.download_button('下載處理後的 XML', modified_xml,
                           file_name=f'processed_{original_name}',
                           mime='application/xml', use_container_width=True)
        with st.expander('預覽 XML（前 2,000 字）'):
            st.code(modified_xml[:2000], language='xml')


def render_excel_tab():
    st.subheader('Excel 資產負債表｜公開上網版轉換')
    st.info('移除「遞延資產」及「負債準備」的明細子科目，保留父科目、彙總金額及來源列印縮放設定。')
    st.caption('公開輸出只含轉換後的單一工作表，不會帶出原始工作表、參考工作表或刪除明細。')
    upload = st.file_uploader('選擇 Excel 原始檔（.xlsx）', type=['xlsx'], key='xlsx_input')
    if upload is None:
        return
    from io import BytesIO
    from openpyxl import load_workbook
    import hashlib
    import os
    from excel_public_converter import convert_excel_public
    raw = upload.getvalue()
    try:
        probe = load_workbook(BytesIO(raw), read_only=True, data_only=False)
        sheet_names = probe.sheetnames
        probe.close()
    except Exception as exc:
        st.error(f'Excel 無法讀取：{exc}')
        return
    source_name = st.selectbox('原始版工作表', sheet_names, index=0, key='xlsx_source')
    ref_options = ['（不使用參考工作表）'] + [n for n in sheet_names if n != source_name]
    suggested = next((i for i, n in enumerate(ref_options) if '公開' in n), 0)
    reference_name = st.selectbox('參考公開版（選填，逐格比對）',
                                  ref_options, index=suggested, key='xlsx_ref')
    ref = reference_name if reference_name != ref_options[0] else None
    opt_a, opt_b = st.columns(2)
    with opt_a:
        keep_layout = st.checkbox('保留原始分頁', value=True, key='xlsx_layout',
                                  help='依桌面版 Excel 的原始分頁，將刪除列的高度平均分給同頁保留的科目列。')
    with opt_b:
        fill_zero = st.checkbox('將數字欄空白補為 0', value=False, key='xlsx_zero')
    fingerprint = hashlib.sha256(raw + repr((source_name, ref, keep_layout, fill_zero)).encode('utf-8')).hexdigest()
    result = st.session_state.get('excel_result')
    if result and result[0] != fingerprint:
        st.session_state.pop('excel_result')
        result = None
    if st.button('產生 Excel 公開上網版', type='primary', use_container_width=True, key='run_xlsx'):
        try:
            public_bytes, report = convert_excel_public(raw, source_sheet=source_name,
                                reference_sheet=ref, fill_zero=fill_zero,
                                preserve_layout=keep_layout)
        except Exception as exc:
            st.error(f'轉換已停止：{exc}')
        else:
            st.session_state['excel_result'] = (fingerprint, public_bytes, report)
            result = st.session_state['excel_result']
    if result:
        _, public_bytes, report = result
        st.success('公開版已完成。原始檔不會被覆蓋。')
        c1, c2, c3 = st.columns(3)
        c1.metric('刪除子科目', f'{len(report.removed)} 筆')
        c2.metric('補入 0 值', f'{report.filled_zero_count} 格')
        c3.metric('對照版逐格差異', '未提供' if report.reference_mismatch_count is None
                  else str(report.reference_mismatch_count))
        if report.adjusted_print_pages:
            st.caption(f'已有 {report.adjusted_print_pages} 頁由同頁的科目列平均分攤刪除列的高度。')
        if report.applied_reference_heights:
            st.caption('參考版逐格一致，已套用其列高。')
        if report.drawing_preserved:
            st.caption('已保留頁首文字方塊（例如「單位：新臺幣元」）。')
        if report.footer_notes_preserved:
            st.caption('已保留頁尾附註圖片。')
        for warning in report.warning_messages:
            st.warning(warning)
        if report.reference_mismatches:
            with st.expander('內容差異（前 30 筆）'):
                for issue in report.reference_mismatches:
                    st.write(issue)
        with st.expander('本次刪除科目（僅畫面顯示，不會寫進公開檔）'):
            st.table([{'父科目': item['parent'], '原始列': item['original_row'],
                       '子科目': item['label']} for item in report.removed])
        base_name = os.path.splitext(upload.name)[0]
        st.download_button('下載 Excel 公開上網版（僅公開內容）', public_bytes,
                           file_name=f'{base_name}_公開上網版.xlsx',
                           mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                           use_container_width=True)


def main():
    st.set_page_config(page_title='XML / Excel 自動化工具', layout='wide')
    st.title('XML / Excel 自動化工具')
    xml_tab, xlsx_tab = st.tabs(['XML 科目處理', 'Excel 公開版轉換'])
    with xml_tab:
        render_xml_tab()
    with xlsx_tab:
        render_excel_tab()
    st.divider()
    st.caption('Excel 資產負債表公開版應在正式上網前，以 Excel 列印預覽確認頁末科目。')


if __name__ == '__main__':
    main()
