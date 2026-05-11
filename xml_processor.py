# 導入需要的函式庫
import streamlit as st
import xml.etree.ElementTree as ET
from io import StringIO

def process_xml(xml_string):
    """
    處理 XML 字串：
    1. 刪除指定範圍的科目編號 ROW。
    2. 重新編號 SEQNO。
    3. 數值調整：
       - <本年度決算數>、<上年度決算數> 負數轉正數。
       - 若上述欄位有負轉正發生，則 <比較增減-金額> 正負號反轉。
       - 排除特定科目名稱。
    """
    try:
        # 為了處理檔案開頭的空白字元，我們先將字串標準化
        xml_string = xml_string.strip()
        
        # 使用 StringIO 將字串偽裝成檔案，讓 ElementTree 可以解析
        it = ET.iterparse(StringIO(xml_string))
        for _, el in it:
            if '}' in el.tag:
                el.tag = el.tag.split('}', 1)[1]  # 去除命名空間
        root = it.root

        # 找到包含所有 ROW 的 DataSet 標籤
        dataset = root.find('DataSet')
        if dataset is None:
            st.error("錯誤：在檔案中找不到 <DataSet> 標籤。請確認檔案格式是否正確。")
            return None, 0

        # --- 第一階段：刪除指定範圍與數值處理 ---
        rows_to_remove = []
        
        for row in dataset.findall('ROW'):
            account_id_el = row.find('科目編號')
            account_name_el = row.find('科目名稱')
            
            # 1. 檢查是否需要刪除
            if account_id_el is not None and account_id_el.text:
                try:
                    account_id = int(account_id_el.text)
                    if (190201 <= account_id <= 190299) or (280101 <= account_id <= 280199):
                        rows_to_remove.append(row)
                        continue # 如果要刪除，就不進行後續的數值處理
                except ValueError:
                    pass

            # 2. 數值處理邏輯 (排除「確定福利計畫之再衡量數」)
            is_excluded = account_name_el is not None and "確定福利計畫之再衡量數" in (account_name_el.text or "")
            
            if not is_excluded:
                triggered_flip = False # 用來紀錄此科目是否有負數變正數的情況
                
                # 欄位 A: 本年度決算數, 上年度決算數 (負數轉正數)
                for tag in ['本年度決算數', '上年度決算數']:
                    el = row.find(tag)
                    if el is not None and el.text:
                        try:
                            # 移除逗號並轉為數字
                            val = float(el.text.replace(',', ''))
                            if val < 0:
                                el.text = str(abs(val))
                                triggered_flip = True # 標記此科目的決算數曾為負數
                        except ValueError:
                            pass
                
                # 欄位 B: 比較增減-金額 (僅在決算數曾為負數時進行正負號反轉)
                if triggered_flip:
                    diff_el = row.find('比較增減-金額')
                    if diff_el is not None and diff_el.text:
                        try:
                            val = float(diff_el.text.replace(',', ''))
                            diff_el.text = str(val * -1)
                        except ValueError:
                            pass

        # 執行刪除動作
        for row in rows_to_remove:
            dataset.remove(row)

        # --- 第二階段：重新編號 SEQNO ---
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

        # 將處理後的 ElementTree 轉回字串
        modified_xml_string = ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')
        
        return modified_xml_string, len(rows_to_remove)

    except ET.ParseError as e:
        st.error(f"XML 解析錯誤： {e}")
        return None, 0
    except Exception as e:
        st.error(f"發生未預期的錯誤：{e}")
        return None, 0

# --- Streamlit 應用程式介面 ---

st.set_page_config(page_title="XML 專業處理工具", layout="wide")
st.title('🚀 XML 檔案處理工具 (精準翻轉版)')

st.markdown("""
### 系統功能說明：
1. **自動過濾**：移除科目編號 `190201-190299` 及 `280101-280199`。
2. **精準數值調整**：
    - `<本年度決算數>`、`<上年度決算數>`：**負數自動轉為正數**。
    - `<比較增減-金額>`：**僅當上述決算數曾出現負數時，才進行正負號反轉**。
    - *注意：若科目名稱包含「確定福利計畫之再衡量數」則完全不變動該科目數值。*
3. **序號重整**：確保 `<SEQNO>` 保持連續。
""")

uploaded_file = st.file_uploader("請上傳 XML 檔案", type=['xml'])

if uploaded_file is not None:
    xml_content = uploaded_file.getvalue().decode('utf-8')
    
    if st.button('執行自動化處理'):
        with st.spinner('正在分析決算數並進行條件轉換...'):
            modified_xml, removed_count = process_xml(xml_content)
            
            if modified_xml:
                st.success(f"處理完成！共移除 {removed_count} 筆資料，並已完成條件式數值翻轉。")
                
                col1, col2 = st.columns(2)
                with col1:
                    st.download_button(
                        label="💾 下載處理後的 XML",
                        data=modified_xml,
                        file_name=f"processed_{uploaded_file.name}",
                        mime="application/xml"
                    )
                
                with st.expander("檢視修改後的內容 (前 2000 字)"):
                    st.code(modified_xml[:2000], language='xml')