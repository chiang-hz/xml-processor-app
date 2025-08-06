# 導入需要的函式庫
import streamlit as st
import xml.etree.ElementTree as ET
from io import StringIO

def process_xml(xml_string):
    """
    處理 XML 字串，刪除指定範圍的科目編號 ROW，並重新編號。

    Args:
        xml_string (str): 包含 XML 內容的字串。

    Returns:
        str: 處理完成的 XML 字串。
        int: 被刪除的 ROW 數量。
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

        # 建立一個列表來存放需要被刪除的 ROW
        rows_to_remove = []
        
        # 遍歷 DataSet 中的所有 ROW
        for row in dataset.findall('ROW'):
            account_id_element = row.find('科目編號')
            if account_id_element is not None and account_id_element.text:
                try:
                    account_id = int(account_id_element.text)
                    
                    # 檢查科目編號是否在任一個指定範圍內
                    if (190201 <= account_id <= 190299) or \
                       (280101 <= account_id <= 280199):
                        rows_to_remove.append(row)

                except ValueError:
                    continue
        
        # 實際執行刪除操作
        for row in rows_to_remove:
            dataset.remove(row)

        # 重新編號所有剩餘的 ROW
        all_remaining_rows = dataset.findall('ROW')
        if all_remaining_rows:
            try:
                start_seq_num = int(all_remaining_rows[0].find('SEQNO').text)
                for index, row in enumerate(all_remaining_rows):
                    seq_element = row.find('SEQNO')
                    if seq_element is not None:
                        seq_element.text = str(start_seq_num + index)
            except (ValueError, TypeError):
                st.warning("警告：無法讀取原始序號，將從 1 開始重新編號。")
                for index, row in enumerate(all_remaining_rows):
                    seq_element = row.find('SEQNO')
                    if seq_element is not None:
                        seq_element.text = str(index + 1)

        # 將修改後的 XML 結構轉換回字串
        modified_xml_string = ET.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')
        
        return modified_xml_string, len(rows_to_remove)

    except ET.ParseError as e:
        st.error(f"XML 解析錯誤： {e}。請上傳一個格式正確的 XML 檔案。")
        return None, 0
    except Exception as e:
        st.error(f"發生未預期的錯誤：{e}")
        return None, 0


# --- Streamlit 應用程式介面 ---

st.title('XML 檔案處理工具')

st.write("""
這個工具可以幫助您處理特定格式的 XML 檔案。

**功能：**
1.  **刪除資料**：自動刪除 `<科目編號>` 介於 **190201-190299** 以及 **280101-280199** 之間的所有 `<ROW>`。
2.  **重新編號**：完成刪除後，會自動更新 `<SEQNO>` 使其保持連續。

請上傳您的 XML 檔案以開始。
""")

uploaded_file = st.file_uploader("選擇一個 XML 檔案", type=['xml'])

if uploaded_file is not None:
    xml_content = uploaded_file.getvalue().decode('utf-8')
    
    st.info("檔案已成功上傳。點擊下方按鈕開始處理。")
    
    if st.button('處理並顯示結果'):
        with st.spinner('正在處理中，請稍候...'):
            modified_xml, rows_deleted_count = process_xml(xml_content)
        
        if modified_xml:
            st.success(f"處理完成！總共刪除了 {rows_deleted_count} 個符合條件的 ROW。")
            
            st.info("您可以直接下載檔案，或在下方預覽內容並複製。")

            # *** 修改點：將下載按鈕移到這裡 ***
            new_file_name = f"processed_{uploaded_file.name}"
            
            st.download_button(
               label="下載修改後的 XML 檔案",
               data=modified_xml,
               file_name=new_file_name,
               mime="application/xml"
            )

            # 將顯示/複製功能放在下載按鈕之後
            st.code(modified_xml, language='xml')