import asyncio
import logging
import random




class ExponentialBackoff:
    """Exponential backoff helper.
    
    Handles retry logic while avoiding system overload and the thundering herd problem.
    Particularly suited to network reconnects and API retries.
    """
    
    def __init__(self, base_delay=30, max_delay=300, max_retries=10, jitter=True):
        """
        Args:
            base_delay (float): Base delay (seconds)
            max_delay (float): Maximum delay (seconds)
            max_retries (int): Maximum number of retries
            jitter (bool): Whether to add random jitter
        """
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.jitter = jitter
        self.retry_count = 0
    
    async def wait(self, context_name=""):
        """Wait, then return whether the counter was reset.
        
        Args:
            context_name (str): Context label used in log messages
            
        Returns:
            bool: Whether the counter was reset
        """
        self.retry_count += 1
        
        # Wait time: base delay * 2^(retry count - 1)
        wait_time = min(self.max_delay, self.base_delay * (2 ** (self.retry_count - 1)))
        
        # Add random jitter (so multiple instances don't retry at the same moment)
        if self.jitter:
            jitter_amount = wait_time * 0.1 * (random.random() * 2 - 1)  # -10% to +10%
            wait_time += jitter_amount
        
        logging.info(f"{context_name}exponential backoff: retrying in {wait_time:.1f}s (attempt {self.retry_count})")
        await asyncio.sleep(wait_time)
        
        # Check whether a reset is needed
        if self.retry_count >= self.max_retries:
            logging.warning(f"{context_name}retry limit ({self.max_retries}) reached; resetting counter")
            self.reset()
            return True
        
        return False
    
    def reset(self):
        """Reset the counter."""
        self.retry_count = 0
    
    def success(self):
        """Mark success and reset the counter."""
        self.reset()
    
    @property
    def current_retry_count(self):
        """Current retry count."""
        return self.retry_count
    
    def __str__(self):
        """String representation."""
        return f"ExponentialBackoff(retry_count={self.retry_count}/{self.max_retries}, base_delay={self.base_delay}s)" 