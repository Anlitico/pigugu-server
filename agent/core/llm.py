# agent/llm.py
"""
Large Language Model (LLM) Module

Base class and implementations for various LLM providers.
Reference: https://docs.livekit.io/agents/models/llm/plugins/openai/
"""

import os
from abc import ABC, abstractmethod
from typing import Optional, Any
from loguru import logger

from livekit.agents import llm
from livekit.agents.llm import ChatContext
from livekit.plugins import openai


class LLMProvider(ABC):
    """
    Abstract base class for Large Language Model providers
    
    This base class defines the interface that all LLM implementations must follow.
    Subclasses should implement the initialization and plugin retrieval methods.
    """
    
    @abstractmethod
    def __init__(self, **kwargs):
        """Initialize the LLM provider with configuration parameters"""
        pass
    
    @abstractmethod
    def get_plugin(self) -> llm.LLM:
        """
        Get the LiveKit plugin instance for use with LiveKit Agent
        
        Returns:
            The underlying LLM plugin instance compatible with LiveKit Agents
            (livekit.agents.llm.LLM)
        """
        pass
    
    @property
    @abstractmethod
    def model(self) -> str:
        """Return the model name being used"""
        pass

    def get_search_adapter(self) -> Any:
        """Return provider-specific search adapter."""
        return None


class QwenLLM(LLMProvider):
    """
    Qwen (Alibaba DashScope) LLM implementation using OpenAI plugin
    
    Uses DashScope's OpenAI-compatible API endpoint with the OpenAI plugin.
    Reference: https://help.aliyun.com/zh/model-studio/getting-started/compatibility-of-openai-with-dashscope/
    """
    
    def __init__(
        self,
        model: str = "qwen-plus",
        temperature: float = 0.8,
        instructions: str = "",
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ):
        """
        Initialize Qwen LLM using OpenAI plugin with DashScope endpoint
        
        Args:
            model: Model name (default: "qwen-plus")
                   Available: qwen-plus, qwen-turbo, qwen-max, qwen-vl-max, etc.
            temperature: Sampling temperature 0-2 (default: 0.8)
            instructions: System instructions/personality (sets up initial ChatContext with system message)
            max_tokens: Maximum tokens in response (optional)
            api_key: DashScope API key (reads from DASHSCOPE_API_KEY env var if not provided)
            base_url: DashScope OpenAI-compatible endpoint
        
        Note:
            The instructions are wrapped in a ChatContext with a system role message.
            This initial context can be accessed via the `initial_chat_ctx` property
            and passed to the Agent for consistent system behavior.
        
        Reference:
            https://help.aliyun.com/zh/model-studio/getting-started/compatibility-of-openai-with-dashscope/
        """
        self._model = model
        self._temperature = temperature
        self._instructions = instructions
        self._max_tokens = max_tokens
        self._base_url = base_url
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        
        if not self.api_key:
            logger.error("DASHSCOPE_API_KEY not set!")
            raise ValueError("DASHSCOPE_API_KEY is required for Qwen LLM")
        
        try:
            # Use OpenAI plugin with custom endpoint and API key
            llm_kwargs = {
                "model": model,
                "temperature": temperature,
                "base_url": base_url,
                "api_key": self.api_key
            }
            
            # Store max_tokens separately - it's passed during chat() not __init__
            self._max_tokens = max_tokens
            
            self._llm = openai.LLM(**llm_kwargs)
            
            # Set up initial chat context with system instructions if provided
            if instructions:
                self._initial_chat_ctx = ChatContext()
                self._initial_chat_ctx.add_message(
                    role="system",
                    content=instructions
                )
                logger.info(f"✅ Initialized Qwen LLM with system prompt")
            else:
                self._initial_chat_ctx = None
                logger.info(f"✅ Initialized Qwen LLM")
            
            logger.info(f"   Model: {model}")
            logger.info(f"   Temperature: {temperature}")
            logger.info(f"   Endpoint: {base_url}")
            if max_tokens:
                logger.info(f"   Max Tokens: {max_tokens}")
            if instructions:
                preview = instructions[:100] + "..." if len(instructions) > 100 else instructions
                logger.info(f"   System Prompt: {preview}")
            
        except ImportError:
            logger.error("livekit-plugins-openai not installed!")
            logger.error("Install with: uv add 'livekit-plugins-openai~=0.6'")
            raise
    
    def get_plugin(self) -> llm.LLM:
        """Get the LiveKit LLM plugin instance"""
        return self._llm
    
    @property
    def model(self) -> str:
        """Return the model name being used"""
        return self._model
    
    @property
    def instructions(self) -> str:
        """Return the system instructions/personality"""
        return self._instructions
    
    @property
    def initial_chat_ctx(self) -> Optional[ChatContext]:
        """Return the initial chat context with system prompt"""
        return self._initial_chat_ctx

    def get_search_adapter(self) -> Any:
        from .search_adapter import create_search_adapter

        if "api.x.ai" in self._base_url:
            return create_search_adapter("grok")
        return create_search_adapter("qwen")


def create_llm(
    model: str = "qwen-plus",
    temperature: float = 0.8,
    instructions: str = "",
    max_tokens: Optional[int] = None,
    api_key: Optional[str] = None,
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
) -> LLMProvider:
    """
    Factory function to create Qwen LLM provider using OpenAI plugin
    
    Uses DashScope's OpenAI-compatible endpoint with the OpenAI plugin.
    
    Args:
        model: Qwen model name (default: "qwen-plus")
               Available: qwen-plus, qwen-turbo, qwen-max, qwen-long, etc.
        temperature: Sampling temperature 0-2 (default: 0.8)
        instructions: System instructions/personality
        max_tokens: Maximum tokens in response (optional)
        api_key: DashScope API key (optional, reads from DASHSCOPE_API_KEY env if not provided)
        base_url: DashScope OpenAI-compatible endpoint
    
    Returns:
        QwenLLM instance
    
    Reference:
        https://help.aliyun.com/zh/model-studio/getting-started/compatibility-of-openai-with-dashscope/
    
    Example - Adding a new provider:
        To add a new LLM provider, create a class that inherits from LLMProvider:
        
        ```python
        from livekit.agents import llm
        
        class NewLLM(LLMProvider):
            def __init__(self, model: str = "model-name", temperature: float = 0.8):
                self._model = model
                self._llm = openai.LLM(
                    model=model,
                    temperature=temperature,
                    base_url="https://api.provider.com/v1",
                    api_key=os.getenv("PROVIDER_API_KEY")
                )
            
            def get_plugin(self) -> llm.LLM:
                return self._llm
            
            @property
            def model(self) -> str:
                return self._model
        ```
    """
    # Determine provider from base_url for clearer logging
    if "api.x.ai" in base_url:
        provider_name = "Grok (xAI)"
    elif "dashscope" in base_url:
        provider_name = "Qwen"
    else:
        provider_name = "OpenAI-compatible"

    logger.info(f"Creating {provider_name} LLM: model={model}, temperature={temperature}")

    return QwenLLM(
        model=model,
        temperature=temperature,
        instructions=instructions,
        max_tokens=max_tokens,
        api_key=api_key,
        base_url=base_url
    )

