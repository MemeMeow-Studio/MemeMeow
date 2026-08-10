"""旧启动文件兼容壳。

Streamlit 生产入口已停用；保留该文件只为让旧启动命令给出明确迁移提示，并启动新的 FastAPI 应用。
"""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8275)
