import sys
from runner.runner import Runner

def main():

    if len(sys.argv) < 2:
        print("Usage: python main.py \"goal\"")
        return

    goal = sys.argv[1]

    runner = Runner()
    runner.run(goal)


if __name__ == "__main__":
    main()