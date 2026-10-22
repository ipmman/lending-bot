import logging
from datetime import datetime, timedelta


def format_elapsed_time(time_diff):
    """
    Format a time-difference dict as a readable string.
    
    :param time_diff: time-difference dict returned by get_elapsed_time
    :return: formatted time string
    """
    days = time_diff["days"]
    hours = time_diff["hours"]
    minutes = time_diff["minutes"]
    seconds = time_diff["seconds"]
    
    # Build the time string
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{seconds}s")
    
    if not parts:
        return "< 1s"
    
    # Format as "1d 2h 3m 4s"
    return " ".join(parts)


def get_elapsed_time(created_timestamp_ms):
    """
    Compute the time elapsed from creation until now.
    
    :param created_timestamp_ms: creation time as a timestamp in milliseconds
    :return: time-difference dict with days, hours, minutes and seconds
    """
    # Current time as a timestamp in milliseconds
    current_timestamp_ms = round(datetime.now().timestamp() * 1000)

    # Time difference (ms)
    time_difference_ms = current_timestamp_ms - created_timestamp_ms
    # Handle negative differences (possibly caused by clock skew)
    if time_difference_ms < 0:
        logging.warning(f"Negative time difference detected: {time_difference_ms/1000:.3f}s; server and local clocks may be out of sync")
        # Treat small negative values as zero; larger ones may need investigating
        if time_difference_ms > -5000:  # Under 5 s: treat as roughly in sync
            time_difference_ms = 0
            
    # Convert milliseconds to a timedelta
    time_difference = timedelta(milliseconds=time_difference_ms)

    # Extract days, hours, minutes and seconds
    days = time_difference.days
    hours, remainder = divmod(time_difference.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    # Total seconds, for easy threshold checks
    total_seconds = time_difference.total_seconds()

    return {
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_seconds": total_seconds
    }



if __name__ == "__main__":
    # Example usage
    created_timestamp_ms = 1692844800000  # Replace with the actual creation timestamp (ms)
    time_diff = get_elapsed_time(created_timestamp_ms)
    print(format_elapsed_time(time_diff))
    
    # Example check: re-place the offer once it is older than 3 hours (10800 s)
    threshold_seconds = 3 * 3600  # 3 hours in seconds
    if time_diff["total_seconds"] >= threshold_seconds:
        print("Offer is older than 3 hours; consider re-placing it")
    else:
        print("Offer age is under 3 hours")
