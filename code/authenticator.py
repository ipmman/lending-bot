from typing import Dict, Any, Optional, List
from datetime import datetime
from websocket_client import WebSocketClient, MessageDispatcher
import hmac
import hashlib
import logging



class Authenticator:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key: str = api_key
        self.api_secret: str = api_secret


    def _build_authentication_message(self, auth_filter: Optional[List[str]] = None) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise ValueError("API key and secret must be provided for authentication.")

        message = {
            "event": "auth",
            "apiKey": self.api_key,
            "authNonce": round(datetime.now().timestamp() * 1_000),
            "authPayload": f"AUTH{round(datetime.now().timestamp() * 1_000)}"
        }
        message["authSig"] = hmac.new(
            key=self.api_secret.encode("utf8"),
            msg=message["authPayload"].encode("utf8"),
            digestmod=hashlib.sha384
        ).hexdigest()
        if auth_filter:
            message["filter"] = auth_filter

        return message


    async def authenticate(self, websocket_client: WebSocketClient, 
                           auth_filter: Optional[List[str]] = None,
                           verbose: bool = False,
                           dispatcher: MessageDispatcher = None) -> bool:
        
        auth_message = self._build_authentication_message(auth_filter=auth_filter)
        await websocket_client.send(auth_message)
        
        while True:
            data = await dispatcher.auth_queue.get()  # 從隊列獲取消息
            if isinstance(data, dict) and data.get("event") == "auth":
                if data.get("status") != "OK":
                    raise Exception("Cannot authenticate with the given API key and secret.")
                if verbose:
                    logging.info(f"Successful login for user <{data.get('userId')}>.")
    
                return True
    
            return False
    