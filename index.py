import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

# 這是你的主應用程式檔名
APP_FILE = "xml_processor.py"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 啟動 Streamlit 應用
        # 使用 os.path.join 確保路徑正確
        streamlit_command = f"streamlit run {os.path.join(os.path.dirname(__file__), APP_FILE)} --server.port $PORT --server.headless true"
        
        # 使用 subprocess 執行指令
        proc = subprocess.Popen(streamlit_command, shell=True)
        proc.wait() # 等待程序完成
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Streamlit server is running.')

if __name__ == '__main__':
    # 這部分主要用於本地測試，Vercel 不會直接執行它
    port = int(os.environ.get('PORT', 8000))
    server_address = ('', port)
    httpd = HTTPServer(server_address, handler)
    print(f'Starting httpd server on port {port}')
    httpd.serve_forever()