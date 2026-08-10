from langchain_community.utilities import SearxSearchWrapper
from langchain_community.document_loaders import WebBaseLoader
from config.settings import Config
from openai import OpenAI
import time
import re
import json
from langchain.schema import HumanMessage
from langchain_openai import ChatOpenAI

def normalize_newlines(content: str) -> str:
    """将文本中连续的多个换行符替换为单个换行符"""
    return re.sub(r'\n+', '\n', content)

# 初始化搜索工具
search = SearxSearchWrapper(searx_host="http://localhost:9090", k=15, engines=["baidu", '!wikipedia'])

# 初始化AI客户端
client = OpenAI(
    api_key=Config().api.embedding_models.api_key,
    base_url="https://api.siliconflow.cn/v1"  # 硅基流动的API地址
)

# 初始化LangChain的ChatOpenAI
llm = ChatOpenAI(
    api_key=Config().api.embedding_models.api_key,
    base_url="https://api.siliconflow.cn/v1",
    model_name='Qwen/Qwen2.5-7B-Instruct'
)

def ask_ai(prompt: str) -> str:
    """使用AI模型处理提示"""
    response = client.chat.completions.create(
        model='Qwen/Qwen2.5-7B-Instruct',  # 指定模型
        messages=[{"role": "user", "content": prompt}]
    )
    # 解析响应内容
    content = ""
    if response.choices:
        for choice in response.choices:
            content += choice.message.content
    return content

def search_web(query: str, num_results=15):
    """使用Searx搜索网页"""
    results = search.results(
        query,
        num_results=num_results,
    )
    return results

def extract_web_content(urls):
    """从URL列表中提取网页内容"""
    all_content = []
    for url in urls:
        try:
            loader = WebBaseLoader(url)
            documents = loader.load()
            content = documents[0].page_content
            if r'知乎，让每一次点击都充满意义' in content:
                continue
            content = normalize_newlines(content)
            all_content.append({"url": url, "content": content})
        except Exception as e:
            print(f"提取网页内容时出错 {url}: {e}")
    return all_content

def search_and_extract(keyword, num_results=10):
    """搜索关键词并提取相关网页内容"""
    print(f"正在搜索关键词: {keyword}")
    
    # 搜索网页
    search_results = search_web(keyword, num_results)
    
    # 提取URL
    urls = [result["link"] for result in search_results]
    print(f"找到 {len(urls)} 个相关网页")
    
    # 提取网页内容
    web_contents = extract_web_content(urls)
    
    return web_contents

# 定义函数工具
def web_search(query: str) -> str:
    """搜索网络获取信息"""
    results = search_and_extract(query, num_results=15)
    all_texts = "\n\n".join([r["content"] for r in results])
    return all_texts[:4000]  # 限制返回内容长度

# 使用function calling实现联网搜索
def search_with_function_calling(target: str):
    """使用function calling实现联网搜索"""
    # 定义工具
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜索网络获取关于特定主题的信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索查询词"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]
    
    # 第一步：生成搜索查询
    messages = [HumanMessage(content=f"我想了解关于'{target}'的信息，请帮我生成一个合适的搜索查询。")]
    response = llm.invoke(messages, tools=tools, tool_choice={"type": "function", "function": {"name": "web_search"}})
    
    # 解析工具调用
    if hasattr(response, 'tool_calls') and response.tool_calls:
        tool_call = response.tool_calls[0]
        if tool_call['name'] == "web_search":
            args = tool_call['args']
            search_query = args.get("query")
            print(f"生成的搜索查询: {search_query}")
            
            # 执行搜索
            search_results = web_search(search_query)
            
            # 第二步：总结搜索结果
            summary_messages = [
                HumanMessage(content=f"请根据以下网页内容，总结关于'{target}'的主要观点和信息：\n\n{search_results}")
            ]
            summary_response = llm.invoke(summary_messages)
            summary = summary_response.content
            
            # 第三步：生成关键词列表
            keywords_messages = [
                HumanMessage(content=f"网络信息：{summary}\n任务：用10个关键词列表介绍[{target}]基本信息和其特点，要求是白话，不能有网络词汇，前五个介绍关键词，后五个情感导向为负面，纯JSON字符串数组单行格式")
            ]
            keywords_response = llm.invoke(keywords_messages)
            
            return {
                "search_query": search_query,
                "summary": summary,
                "keywords": keywords_response.content
            }
    
    return {"error": "无法执行搜索"}

if __name__ == "__main__":
    start_time = time.time()
    # 用户输入关键词
    target = "如何评价张维为"
    
    # 使用function calling实现联网搜索
    result = search_with_function_calling(target)
    
    print("\n--- 搜索查询 ---")
    print(result.get("search_query", "无搜索查询"))
    
    print("\n--- AI总结 ---")
    print(result.get("summary", "无总结"))
    
    print("\n--- 关键词列表 ---")
    print(result.get("keywords", "无关键词"))
    
    end_time = time.time()
    print(f"\n总耗时: {end_time - start_time:.2f} 秒")

# 注释掉旧代码
# searchs = ask_ai(f"'假设你是一个网友，提取[{target}]你不认识的一个网络词汇，组成'XXX是什么'的搜索句子，用于浏览器搜索，只需给出你的句子即可，不要有多余的话'")
# # searchs = target
# print(searchs)
# result = get_web_data(searchs)
# print(result)
# ans = ask_ai(f"网络信息：{result}\n任务：用10个关键词列表介绍[{target}]基本信息和其特点，要求是白话，不能有网络词汇，前五个介绍关键词，后五个情感导向为负面，纯JSON字符串数组单行格式")
# end = time.time()
# print(ans)
# print((end-start))