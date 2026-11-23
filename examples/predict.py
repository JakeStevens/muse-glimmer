"""Send one prompt to Glimmer and print its reply and server timings."""

import argparse

from muse_glimmer import Glimmer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", default="Suggest a name for a coffee shop on the moon.")
    args = parser.parse_args()

    g = Glimmer(timings=True)
    print(g.predict(args.prompt))


if __name__ == "__main__":
    main()
