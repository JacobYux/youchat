import sys

if sys.version_info < (3, 8):  # checked before importing modules that need 3.8 syntax
    sys.exit("youchat needs Python >= 3.8 (this is %s). Run it with a newer interpreter, e.g.:\n"
             "  python3.12 %s %s" % (sys.version.split()[0], sys.argv[0], " ".join(sys.argv[1:])))

from youchat.cli import main  # noqa: E402

main()
