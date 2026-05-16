# Hand-written stub for protos/umich_consumer_sentiment.proto.
from dataclasses import dataclass

PRELIMINARY = 0
FINAL = 1


@dataclass
class UmichConsumerSentimentRecord:
    """Schema stub matching UmichConsumerSentimentRecord in umich_consumer_sentiment.proto."""

    survey_month: str = ""
    reading_type: int = 0
    index_value: float = 0.0
    expectations_index: float = 0.0
    current_conditions_index: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
