import os
from getpass import getpass

import jevdb

# 使用 JEV or OPENAI
# OpenAI 兼容后端读取环境变量 OPENAI_API_KEY；请勿在代码中写入 Key。
if not os.environ.get("JEV_API_KEY"):
    os.environ["JEV_API_KEY"] = getpass("JEV_API_KEY: ").strip()

con = jevdb.connect()
try:
    # con.execute("CREATE SECRET llm (TYPE openai, PROVIDER env)")
    # # 按需求修改 endpoint 和 model，运行脚本后输入 api key
    # con.execute("SET jevdb_backend = 'llm'")
    # con.execute("SET jevdb_endpoint = 'https://open.bigmodel.cn/api/coding/paas/v4'")
    # con.execute("SET jevdb_model = 'glm-5.3'")

    con.execute("CREATE SECRET jev (TYPE jev, PROVIDER env)")
    con.execute("SET jevdb_endpoint = 'https://app.vllm-sr.ai/v1'")
    con.execute("SET jevdb_model = 'vllm-sr/d3-ultra'")

    rows = con.execute("""
        SELECT jev_holds(
            'The review is clearly positive',
            'Loved every minute of it.'
        )
    """).fetchall()
    print(rows)
finally:
    con.close()