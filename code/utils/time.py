import logging
from datetime import datetime, timedelta


def format_elapsed_time(time_diff):
    """
    將時間差字典格式化為美觀的字符串
    
    :param time_diff: 由get_elapsed_time返回的時間差字典
    :return: 格式化的時間字符串
    """
    days = time_diff["days"]
    hours = time_diff["hours"]
    minutes = time_diff["minutes"]
    seconds = time_diff["seconds"]
    
    # 構建時間字符串
    parts = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0 or days > 0:
        parts.append(f"{hours}小時")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}分鐘")
    if seconds > 0 or minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{seconds}秒")
    
    if not parts:
        return "掛單時間: < 1秒"
    
    # 格式化為 "1天 2小時 3分鐘 4秒" 的格式
    return " ".join(parts)


def get_elapsed_time(created_timestamp_ms):
    """
    計算從創建時間到當前時間的時間差。
    
    :param created_timestamp_ms: 創建時間的毫秒時間戳
    :return: 包含天、時、分、秒的時間差字典
    """
    # 當前時間的毫秒時間戳
    current_timestamp_ms = round(datetime.now().timestamp() * 1000)

    # 計算時間差（毫秒）
    time_difference_ms = current_timestamp_ms - created_timestamp_ms
    # 處理負值情況（可能由於時鐘不同步造成）
    if time_difference_ms < 0:
        logging.warning(f"檢測到負時間差: {time_difference_ms/1000:.3f}秒，可能是伺服器時間與本地時間不同步")
        # 輕微的負值直接視為零，較大的負值可能需要調查
        if time_difference_ms > -5000:  # 如果小於5秒，視為近似同步
            time_difference_ms = 0
            
    # 將毫秒轉換為 timedelta 對象
    time_difference = timedelta(milliseconds=time_difference_ms)

    # 提取天、時、分、秒
    days = time_difference.days
    hours, remainder = divmod(time_difference.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    # 取得總秒數，方便邏輯判斷
    total_seconds = time_difference.total_seconds()

    return {
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_seconds": total_seconds
    }



if __name__ == "__main__":
    # 示例使用
    created_timestamp_ms = 1692844800000  # 替換為實際的創建時間毫秒時間戳
    time_diff = get_elapsed_time(created_timestamp_ms)
    print(format_elapsed_time(time_diff))
    
    # 邏輯判斷：例如設定超過3小時 (10800秒) 就需要重新掛單
    threshold_seconds = 3 * 3600  # 3小時的秒數
    if time_diff["total_seconds"] >= threshold_seconds:
        print("掛單超過3小時，考慮重新掛單")
    else:
        print("掛單時間未超過3小時")
