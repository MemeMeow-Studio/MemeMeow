from langchain_community.utilities import SearxSearchWrapper
from langchain_community.document_loaders import WebBaseLoader
from config.settings import Config
from openai import OpenAI
import time
import re
from loguru import logger

def normalize_newlines(content: str) -> str:
    """将文本中连续的多个换行符替换为单个换行符"""
    return re.sub(r'\n+', '\n', content)

# 初始化搜索工具
search = SearxSearchWrapper(searx_host="http://localhost:9090", k=5, engines=["baidu", '!wikipedia'])

# 初始化AI客户端
client = OpenAI(
    api_key=Config().api.embedding_models.api_key,
    base_url="https://api.siliconflow.cn/v1"  # 硅基流动的API地址
)

def ask_ai(prompt: str) -> str:
    """使用AI模型处理提示"""
    response = client.chat.completions.create(
        model='deepseek-ai/DeepSeek-R1-Distill-Llama-8B',  # 指定模型
        messages=[{"role": "user", "content": prompt}]
    )
    # 解析响应内容
    content = ""
    if response.choices:
        for choice in response.choices:
            content += choice.message.content
    return content

def search_web(query: str, num_results=5):
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
            logger.exception(e)
            logger.error(f"提取网页内容时出错 {url}: {e}")
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

if __name__ == "__main__":
    start_time = time.time()
    # 用户输入关键词
    target = "如何评价张维为"
    keyword = ask_ai(f"'假设你是一个网友，提取[{target}]你不认识的一个网络词汇，组成'XXX是什么'的搜索句子，用于浏览器搜索，只需给出你的句子即可，不要有多余的话'")
    
    
    # 搜索并提取内容
    results = search_and_extract(keyword)
    
    # 打印结果
    for i, result in enumerate(results):
        print(f"\n--- 网页 {i+1} ---")
        print(f"URL: {result['url']}")
        print(f"内容摘要: {result['content'][:200]}...")
    
    # 使用AI总结内容
    all_texts = "\n\n".join([r["content"] for r in results])
    summary = ask_ai(f"请根据以下网页内容，总结关于'{keyword}'的主要观点和信息：\n\n{all_texts[:4000]}")
    
    print("\n--- AI总结 ---")
    print(summary)
    ans = ask_ai(f"网络信息：{summary}\n任务：用10个关键词列表介绍[{summary}]基本信息和其特点，要求是白话，不能有网络词汇，前五个介绍关键词，后五个情感导向为负面，纯JSON字符串数组单行格式")
    print(ans)
    end_time = time.time()
    
    print(f"\n总耗时: {end_time - start_time:.2f} 秒")

# searchs = ask_ai(f"'假设你是一个网友，提取[{target}]你不认识的一个网络词汇，组成'XXX是什么'的搜索句子，用于浏览器搜索，只需给出你的句子即可，不要有多余的话'")
# # searchs = target
# print(searchs)
# result = get_web_data(searchs)
# print(result)
# ans = ask_ai(f"网络信息：{result}\n任务：用10个关键词列表介绍[{target}]基本信息和其特点，要求是白话，不能有网络词汇，前五个介绍关键词，后五个情感导向为负面，纯JSON字符串数组单行格式")
# end = time.time()
# print(ans)
# print((end-start))