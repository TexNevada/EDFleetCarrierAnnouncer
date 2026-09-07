"""Plugin version.

This is the only file whose contents differ between the main and dev branches, so
merging the two conflicts on exactly one line and nothing else:

    main:  VERSION = "1.0.0"
    dev:   VERSION = "1.0.0-dev"

The value is baked in rather than derived from git because some users install by
downloading a zip from GitHub, which carries no ``.git`` directory.  Releases on
main are tagged ``v<VERSION>``; the update check compares against that tag.
"""

VERSION = "1.0.0-dev"
