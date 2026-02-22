import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from letta_client import Letta

load_dotenv()


def test_letta_connection():
    client = Letta(api_key=os.getenv("LETTA_API_KEY"))

    agents = client.agents.list()

    print("Letta connection successful!")
    print(f"Existing agents: {len(agents)}")


if __name__ == "__main__":
    test_letta_connection()
