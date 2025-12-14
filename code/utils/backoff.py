import asyncio
import logging
import random




class ExponentialBackoff:
    """指數退避策略工具類
    
    用於處理重試邏輯，避免系統過載和雷群效應。
    特別適用於網絡重連、API重試等場景。
    """
    
    def __init__(self, base_delay=30, max_delay=300, max_retries=10, jitter=True):
        """
        Args:
            base_delay (float): 基礎延遲時間（秒）
            max_delay (float): 最大延遲時間（秒）
            max_retries (int): 最大重試次數
            jitter (bool): 是否添加隨機抖動
        """
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.jitter = jitter
        self.retry_count = 0
    
    async def wait(self, context_name=""):
        """執行等待，返回是否應該重置計數器
        
        Args:
            context_name (str): 上下文名稱，用於日誌記錄
            
        Returns:
            bool: 是否重置了計數器
        """
        self.retry_count += 1
        
        # 計算等待時間：基礎延遲 * 2^重試次數
        wait_time = min(self.max_delay, self.base_delay * (2 ** (self.retry_count - 1)))
        
        # 添加隨機抖動（避免多個實例同時重試）
        if self.jitter:
            jitter_amount = wait_time * 0.1 * (random.random() * 2 - 1)  # -10% 到 +10%
            wait_time += jitter_amount
        
        logging.info(f"{context_name}使用指數退避策略，等待 {wait_time:.1f} 秒後重試 (第{self.retry_count}次)")
        await asyncio.sleep(wait_time)
        
        # 檢查是否需要重置
        if self.retry_count >= self.max_retries:
            logging.warning(f"{context_name}重試次數達到上限({self.max_retries})，重置計數器")
            self.reset()
            return True
        
        return False
    
    def reset(self):
        """重置計數器"""
        self.retry_count = 0
    
    def success(self):
        """標記成功，重置計數器"""
        self.reset()
    
    @property
    def current_retry_count(self):
        """獲取當前重試次數"""
        return self.retry_count
    
    def __str__(self):
        """字符串表示"""
        return f"ExponentialBackoff(retry_count={self.retry_count}/{self.max_retries}, base_delay={self.base_delay}s)" 