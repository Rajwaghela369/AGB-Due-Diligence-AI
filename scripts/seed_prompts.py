import argparse

from core import langfuse_client

from app.agent.prompts import seed

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="evaluation")
    args = parser.parse_args()
    seed(langfuse_client.client(), label=args.label)
