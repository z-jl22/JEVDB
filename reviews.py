import os
from getpass import getpass

import jevdb

# 使用 JEV or OPENAI
# OpenAI 兼容后端读取环境变量 OPENAI_API_KEY；请勿在代码中写入 Key。
if not os.environ.get("JEV_API_KEY"):
    os.environ["JEV_API_KEY"] = getpass("JEV_API_KEY: ").strip()

con = jevdb.connect()
try:
    # # 使用 OPENAI
    # con.execute("CREATE SECRET llm (TYPE openai, PROVIDER env)")
    # # 按需求修改 endpoint 和 model，运行脚本后输入 api key
    # con.execute("SET jevdb_backend = 'llm'")
    # con.execute("SET jevdb_endpoint = 'https://open.bigmodel.cn/api/coding/paas/v4'")
    # con.execute("SET jevdb_model = 'glm-5.3'")

    # 使用 JEV
    con.execute("CREATE SECRET jev (TYPE jev, PROVIDER env)")
    con.execute("SET jevdb_endpoint = 'https://app.vllm-sr.ai/v1'")
    con.execute("SET jevdb_model = 'vllm-sr/d3-ultra'")

    con.execute("""
        CREATE TABLE reviews (id INTEGER, review VARCHAR);
        INSERT INTO reviews VALUES
          (1, 'Loved every minute of it.'),
          (2, 'A tedious, overlong mess.'),
          (3, 'Fine, but forgettable.');
    """)

    print("明显正面的影评：")
    print(con.execute("""
        SELECT id FROM reviews
        WHERE jev_holds('The review is clearly positive', review)
    """).fetchall())

    print("概率、分类和评分：")
    print(con.execute("""
        SELECT id,
               jev_probability('The review is clearly positive', review) AS p,
               jev_choose(
                   'Classify the sentiment',
                   review,
                   ['positive', 'negative', 'mixed']
               ) AS label,
               jev_score(
                   'Rate the enthusiasm',
                   review,
                   ['low', 'medium', 'high']
               ) AS enthusiasm
        FROM reviews
    """).fetchall())

    print("情绪相同的影评对：")
    print(con.execute("""
        SELECT a.id, b.id
        FROM reviews a JOIN reviews b
          ON a.id < b.id
         AND jev_holds(
             'The two reviews express the same sentiment',
             a.review, b.review
         )
    """).fetchall())
finally:
    con.close()