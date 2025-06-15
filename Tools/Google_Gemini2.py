from google import genai
from google.genai.types import Tool, GenerateContentConfig, GoogleSearch

client = genai.Client(api_key="Your Key")

google_search_tool = Tool(
    google_search = GoogleSearch()
)

response = client.models.generate_content(model='gemini-2.0-flash-exp', 
                                          contents='How does AI work?',
                                          config=GenerateContentConfig(
        tools=[google_search_tool],
        response_modalities=["TEXT"],
    ))
print(response.text)
