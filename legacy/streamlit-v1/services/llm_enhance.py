from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from duckduckgo_search import DDGS
from langchain_openai import ChatOpenAI
from base import *
from config.settings import Config
def get_web_data(query: str) -> str:
    """使用DuckDuckGo搜索引擎进行搜索"""
    results = DDGS().text(query, max_results=12)
    return results

class LLMEnhance:
    def __init__(self):
        self.llm = ChatOpenAI(
    api_key=Config().api.embedding_models.api_key,
    base_url="https://api.siliconflow.cn/v1",
    model_name='Qwen/Qwen2.5-7B-Instruct'
)

    def search(self, target):
        search_keywords = self.llm.invoke([HumanMessage(content=f"你接下来的任务是[{target}]，但在这之前，你需要搜索相关信息。现在假装你正在搜索网页，直接给出一到两个搜索关键词，不要有多余的话，我会帮你搜索。")])
        logger.debug(search_keywords.content)
        search_result = get_web_data(search_keywords.content)
        logger.debug(f"search_result: {search_result}")
        result = self.llm.invoke([HumanMessage(content=f"网络信息: {search_result}\n\n 用一句长句介绍或回答[{target}]")])
        logger.debug(f"result: {result.content}")
        return result.content

    def judge_possible_memes(self, possible_memes:t.List[str]):
        pass


if __name__ == '__main__':
    llm = LLMEnhance()
    llm.search("如何评价《这就是中国》")

