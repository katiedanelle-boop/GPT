# GPT

## Steam + ProtonDB Export Script

This repository contains `steam_protondb_export.py`, a helper utility for
exporting your Steam library and the corresponding ProtonDB rankings into a
CSV file.

### Prerequisites

* Python 3.9+
* A Steam Web API key (https://steamcommunity.com/dev/apikey)
* The 64-bit SteamID for the account you want to inspect

### Usage

```bash
python steam_protondb_export.py \
  --steamid 76561197960435530 \
  --api-key "$STEAM_API_KEY" \
  --output my_library.csv
```

You can omit the `--api-key` and `--steamid` flags if the values are available
in the `STEAM_API_KEY` and `STEAM_ID` environment variables, respectively.

By default the script sleeps for 0.3 seconds between ProtonDB requests to avoid
rate limiting. Use `--no-rate-limit` to disable this behaviour or `--rate-limit`
to set a custom delay.

The resulting CSV contains one row per owned game and is ordered from the best
ProtonDB tier to the worst. Games without ProtonDB data appear at the bottom of
the export.
