from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from duckduckgo_search import DDGS
from base import *
from config.settings import Config

# 定义搜索工具
@tool
def get_web_data(query: str) -> str:
    """使用DuckDuckGo搜索引擎进行搜索"""
    results = DDGS().text(query, max_results=10)
    return results

# 初始化模型
api_key = Config().api.embedding_models.api_key
base_url = "https://api.siliconflow.cn/v1"
model = init_chat_model("Qwen/Qwen2.5-7B-Instruct", model_provider="openai", api_key=api_key, base_url=base_url)

# 绑定工具
tools = [get_web_data]
llm_with_tools = model.bind_tools(tools)

# 用户查询
query = "母鸡卡第八集"
messages = [SystemMessage("\
你是一位表情包搜索辅助专家。用户会输入一句话，你需要分析用户的输入，分析其中的性质，把它拆解为几个关键词，用于搜索表情包。\
你有联网搜索的工具。请使用工具联网搜索，确保更准确的回答。\
尽量不要输出没有意义的关键词，例如“评论”，“内容”，这些关键词无法寻找到对应的表情包。你应该思考什么关键词容易搜索到表情包。\
例如，如果用户输入“积极评价黑神话悟空”，你应该输出[黑神话悟空，西游记，孙悟空，游戏，积极评价，赞扬]")
    , HumanMessage(query)]

# 调用模型
ai_msg = llm_with_tools.invoke(messages)
print(ai_msg.tool_calls)
if len(ai_msg.tool_calls) == 0:
    print(ai_msg.content)
    exit(0)
# 调用搜索工具
messages.append(ai_msg)
for tool_call in ai_msg.tool_calls:
    selected_tool = {"get_web_data": get_web_data}[tool_call["name"].lower()]
    tool_msg = selected_tool.invoke(tool_call)
    print(tool_msg)
    messages.append(tool_msg)

# 获取最终回答
print(llm_with_tools.invoke(messages).content)
