"""Keep the app's saved data (messages, contacts, drafts, settings, logs) readable only by you."""
import os


def use_private_umask():
    """Files the app creates from now on are owner-only (0600) and directories 0700."""
    os.umask(0o077)


def harden(*roots):
    """Tighten permissions on data written by earlier versions. Missing paths are skipped."""
    for root in roots:
        if not os.path.isdir(root):
            continue
        os.chmod(root, 0o700)
        for base, dirs, files in os.walk(root):
            for name in dirs:
                os.chmod(os.path.join(base, name), 0o700)
            for name in files:
                os.chmod(os.path.join(base, name), 0o600)
