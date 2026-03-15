# Collection Refresh Setup

This sets up a narrow sudo path for reading MTGA collection data from process memory.

## Files

- Wrapper template: `tools/templates/mtga-read-collection.sh`
- Sudoers template: `tools/templates/mtga-read-collection.sudoers`
- Refresh helper: `tools/refresh_collection_snapshot.py`

## Install

1. Install the wrapper as a root-owned executable:

```bash
sudo install -o root -g wheel -m 0755 \
  /Users/vladimirbalko/MTG/scryglass/tools/templates/mtga-read-collection.sh \
  /usr/local/sbin/mtga-read-collection
```

2. Add the sudoers rule with `visudo`:

```bash
sudo visudo -f /etc/sudoers.d/mtga-read-collection
```

Paste:

```sudoers
vladimirbalko ALL=(root) NOPASSWD: /usr/local/sbin/mtga-read-collection
```

3. Lock down the sudoers file:

```bash
sudo chmod 0440 /etc/sudoers.d/mtga-read-collection
```

## Test

The privileged wrapper itself:

```bash
sudo /usr/local/sbin/mtga-read-collection
```

The repo helper that also syncs the repo-local snapshot:

```bash
cd /Users/vladimirbalko/MTG/scryglass
uv run python tools/refresh_collection_snapshot.py
```

## Manage UI

After setup, the Manage page can trigger the refresh through:

- `POST /api/manage/refresh-collection`

That endpoint runs `tools/refresh_collection_snapshot.py`, which in turn calls:

```bash
sudo /usr/local/sbin/mtga-read-collection
```

## Security Note

This is a pragmatic setup, not a perfect one.

The wrapper still executes Python code from a user-writable repo. That is much better than broad sudo access, but it is not as strong as putting the memory reader into a separate root-owned immutable path.

If you want tighter isolation later, move the memory-reader code into a dedicated root-owned directory and point the wrapper there.
