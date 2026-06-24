import argparse


class AgentConfig:
    def __init__(self) -> None:
        parser = argparse.ArgumentParser(description="Parser Agent smith")

        parser.add_argument("--task-file", required=True, help="")
        parser.add_argument("--output", required=True, help="")
        parser.add_argument("--model-name", required=True, help="")
        parser.add_argument("--provider-url", required=True, help="")

        args = parser.parse_args()

        self.task_file = args.task_file
        self.output = args.output
        self.model_name = args.model_name
        self.provider_url = args.provider_url