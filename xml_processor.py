# 導入需要的函式庫
import streamlit as st
import xml.etree.ElementTree as ET
from io import StringIO

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
                account_id_el = row.find('科目編號')
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

st.set_page_config(page_title="XML 自動化工具", layout="centered")

st.title('🚀 XML 檔案處理工具')

# 在畫面中間建立設定區域
st.markdown("### 🛠️ 處理設定")
config_col1, config_col2 = st.columns(2)

with config_col1:
    do_delete = st.toggle("啟用「科目範圍刪除」", value=True, help="刪除科目編號 1902xx 與 2801xx")
    st.caption("自動過濾不必要的科目編號")

with config_col2:
    do_value_adj = st.toggle("啟用「數值符號調整」", value=True, help="處理負數決算數及反轉增減金額")
    st.caption("負數轉正及增減金額反轉邏輯")

st.divider() # 加入分隔線

# 上傳區域
uploaded_file = st.file_uploader("📂 請選擇要處理的 XML 檔案", type=['xml'])

if uploaded_file is not None:
    xml_content = uploaded_file.getvalue().decode('utf-8')
    
    # 執行按鈕
    if st.button('✨ 開始自動化處理', use_container_width=True):
        if not do_delete and not do_value_adj:
            st.warning("⚠️ 目前未開啟任何功能，僅會進行 SEQNO 序號重整。")
        
        with st.spinner('正在分析數據並轉換格式...'):
            modified_xml, removed_count = process_xml(xml_content, do_delete, do_value_adj)
            
            if modified_xml:
                st.success(f"✅ 處理完成！")
                
                # 統計數據顯示
                res_col1, res_col2 = st.columns(2)
                res_col1.metric("刪除行數", f"{removed_count} 筆")
                res_col2.metric("處理狀態", "成功")

                st.download_button(
                    label="💾 下載處理後的 XML 檔案",
                    data=modified_xml,
                    file_name=f"processed_{uploaded_file.name}",
                    mime="application/xml",
                    use_container_width=True
                )
                
                with st.expander("🔍 預覽修改後的代碼 (前 2000 字)"):
                    st.code(modified_xml[:2000], language='xml')

# 頁尾說明
st.markdown("""
---
<p style='text-align: center; color: gray;'>
    💡 提示：修改設定後不需重新上傳，直接點擊「開始自動化處理」即可。
</p>
""", unsafe_allow_html=True)