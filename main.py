import argparse
import json
import os
import time
from functools import wraps
from typing import Any, Callable, Dict, Literal, Tuple, TypeVar

import requests
from tabulate import tabulate

RT = TypeVar("RT")


def cache_with_expiry(seconds: int) -> Callable[[Callable[..., RT]], Callable[..., RT]]:
    cache: Dict[Tuple[Any, frozenset[tuple[str, Any]]], Tuple[float, Any]] = {}

    def decorator(func: Callable[..., RT]) -> Callable[..., RT]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> RT:
            cache_key: Tuple[Any, frozenset[tuple[str, Any]]] = (args, frozenset(kwargs.items()))
            if cache_key in cache:
                cached_time, cached_result = cache[cache_key]
                if time.time() - cached_time < seconds:
                    return cached_result  # type: ignore[no-any-return]

            result: Any = func(*args, **kwargs)
            cache[cache_key] = (time.time(), result)
            return result  # type: ignore[no-any-return]

        return wrapper

    return decorator


def nm_request(endpoint: str, api_key: str, params: dict[str, Any] | None = None) -> Any:
    headers = {"apikey": api_key, "User-Agent": "Nexus Mods Latest Mods Notifier / v0.1.0"}
    url = f"https://api.nexusmods.com/v1/{endpoint}"
    response = requests.get(url, headers=headers, params=params)
    return response.json()


def fetch_latest_mods(api_key: str, game_domain_name: str) -> list[dict[str, Any]]:
    return nm_request(f"games/{game_domain_name}/mods/latest_added.json", api_key)  # type: ignore[no-any-return]


def fetch_tracked_mods(api_key: str, game_domain_name: str = "") -> list[dict[str, Any]]:
    mods: list[dict[str, Any]] = nm_request("user/tracked_mods.json", api_key)
    if game_domain_name:
        mods = [mod for mod in mods if mod["domain_name"] == game_domain_name]
    return mods


def fetch_mod(api_key: str, game_domain_name: str, mod_id: int) -> dict[str, Any]:
    return nm_request(f"games/{game_domain_name}/mods/{mod_id}.json", api_key)  # type: ignore[no-any-return]


def fetch_updated_mods(
    api_key: str, game_domain_name: str, time_period: Literal["1d", "1w", "1m"] = "1w"
) -> list[dict[str, Any]]:
    return nm_request(  # type: ignore[no-any-return]
        f"games/{game_domain_name}/mods/updated.json",
        api_key,
        params={"period": time_period},
    )


def fetch_mod_changelogs(api_key: str, game_domain_name: str, mod_id: int) -> dict[str, list[str]]:
    return nm_request(f"games/{game_domain_name}/mods/{mod_id}/changelogs.json", api_key)  # type: ignore[no-any-return]


def send_telegram_message(chat_id: str, text: str, tg_token: str, topic_id: str) -> None:
    url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if topic_id:
        payload["message_thread_id"] = topic_id
    requests.post(url, data=payload)


def load_state(state_file: str) -> Any:
    if os.path.exists(state_file):
        with open(state_file) as f:
            return json.load(f)


def save_state(state_file: str, seen_mods: Any) -> None:
    with open(state_file, "w") as f:
        json.dump(seen_mods, f)


def additions(
    api_key: str,
    game_domain_name: str,
    chat_id: str,
    tg_token: str,
    hide_adult_content: bool,
    loop: bool,
    topic_id: str,
    frequency: int,
) -> None:
    state_file = "seen_mods.json"
    seen_mods: set[int] = set(load_state(state_file) or [])  # pyright: ignore[reportGeneralTypeIssues]
    new_mods_data = []

    while True:
        print("Starting new mod check...")
        mods = fetch_latest_mods(api_key, game_domain_name)
        for mod in sorted(mods, key=lambda x: x["mod_id"]):
            mod_id = mod["mod_id"]
            if mod_id not in seen_mods:
                if not mod["available"]:
                    print(f"Mod [id={mod_id}] not available yet, skipping...")
                    continue

                seen_mods.add(mod_id)

                if hide_adult_content and mod["contains_adult_content"]:
                    print("Mod contains adult content, skipping...")
                    continue

                new_mod_data = {
                    "ID": mod_id,
                    "Author": mod["author"],
                    "Name": mod.get("name", "N/A"),
                    "Link": f"https://nexusmods.com/{mod['domain_name']}/mods/{mod['mod_id']}",
                }
                new_mods_data.append(new_mod_data)

                send_telegram_message(
                    chat_id=chat_id,
                    text=(
                        f"<b>{mod.get('name', 'N/A')}</b>\n{mod['author']} - Version {mod['version']}\nLink:"
                        f" https://nexusmods.com/{mod['domain_name']}/mods/{mod['mod_id']}"
                    ),
                    tg_token=tg_token,
                    topic_id=topic_id,
                )

        if new_mods_data:
            print("New mods found:")
            print(tabulate(new_mods_data, headers="keys", tablefmt="pretty"))
            new_mods_data.clear()
        else:
            print("No new mods found.")

        save_state(state_file, list(seen_mods))
        if loop:
            print(f"Sleeping for {frequency / 60} minute/s...")
            time.sleep(frequency)
        else:
            break


def updates(
    api_key: str,
    game_domain_name: str,
    chat_id: str,
    tg_token: str,
    hide_adult_content: bool,
    loop: bool,
    topic_id: str,
    frequency: int,
) -> None:
    cache_file_path = "update_cache.json"
    local_cache: dict[int, dict[str, Any]] = {
        int(mod_id): value for mod_id, value in (load_state(cache_file_path) or {}).items()
    }
    tracked_mod_ids: set[int] = set()

    mods_with_new_version: list[dict[str, Any]] = []

    # Initial population of tracked mods (do this carefully to stay within API limits)
    if not local_cache:
        print("Fetching initial list of tracked mods...")
        updated_mods = fetch_updated_mods(api_key, game_domain_name)
        updated_mod_data = {mod["mod_id"]: mod["latest_file_update"] for mod in updated_mods}

        tracked_mods = fetch_tracked_mods(api_key, game_domain_name)
        tracked_mod_ids = {mod["mod_id"] for mod in tracked_mods}
        for mod_id in tracked_mod_ids:
            mod_info = fetch_mod(api_key, game_domain_name, mod_id)
            local_cache[mod_id] = {
                "version": mod_info["version"],
                "is_adult": mod_info["contains_adult_content"],
                "latest_file_update": updated_mod_data.get(mod_id, None),
            }
        save_state(cache_file_path, local_cache)
        print("Initial population of tracked mods complete.")

    while True:
        try:
            print("Starting update check...")
            # Fetch list of all recently updated mods
            updated_mods = fetch_updated_mods(api_key, game_domain_name)
            updated_mod_data = {mod["mod_id"]: mod["latest_file_update"] for mod in updated_mods}

            # Refresh the list of tracked mods
            tracked_mods = fetch_tracked_mods(api_key, game_domain_name)
            tracked_mod_ids = {mod["mod_id"] for mod in tracked_mods if not hide_adult_content or not mod["is_adult"]}

            for mod_id in tracked_mod_ids:
                cached_latest_file_update: int | None = local_cache.get(mod_id, {}).get("latest_file_update", None)
                new_latest_file_update: int | None = updated_mod_data.get(mod_id, None)

                if mod_id not in local_cache:
                    print(f"Tracking new mod [id={mod_id}], fetching...")
                    mod_details = fetch_mod(api_key, game_domain_name, mod_id)
                    local_cache[mod_id] = {
                        "version": mod_details["version"],
                        "latest_file_update": new_latest_file_update,
                        "is_adult": mod_details["contains_adult_content"],
                    }
                    continue

                # If the latest_file_update has changed or is new, there might be a new version
                if new_latest_file_update and cached_latest_file_update != new_latest_file_update:
                    mod_details = fetch_mod(api_key, game_domain_name, mod_id)
                    new_version = mod_details["version"]
                    old_version = local_cache.get(mod_id, {}).get("version", None)

                    if old_version and new_version and old_version != new_version:
                        print(f"Mod [id={mod_id}] has been updated to from {old_version} to {new_version}")
                        changelogs = fetch_mod_changelogs(api_key, game_domain_name, mod_id)
                        last_version_index = (
                            list(changelogs.keys()).index(old_version) if old_version in changelogs else -2
                        )

                        new_versions = dict(list(changelogs.items())[last_version_index + 1 :])

                        changelog_text = "\n".join(
                            "<b>{}</b>\n- {}".format(version, "\n- ".join(changelog))
                            for version, changelog in new_versions.items()
                        )

                        send_telegram_message(
                            chat_id=chat_id,
                            text=(
                                f"<b>{mod_details['name']}</b>\n{mod_details['author']} - Version {old_version} ->"
                                f" {new_version}\n"
                                f"Link: https://nexusmods.com/{mod_details['domain_name']}/mods/{mod_id}\n"
                                f"Changelog:\n{changelog_text}"
                            ),
                            tg_token=tg_token,
                            topic_id=topic_id,
                        )

                        for version in new_versions:
                            mods_with_new_version.append(
                                {
                                    "ID": mod_id,
                                    "Author": mod_details["author"],
                                    "Name": mod_details["name"],
                                    "Link": f"https://nexusmods.com/{mod_details['domain_name']}/mods/{mod_id}",
                                    "Old Version": old_version or "N/A",
                                    "New Version": version,
                                }
                            )

                    local_cache[mod_id] = {
                        "version": new_version,
                        "latest_file_update": new_latest_file_update,
                        "is_adult": mod_details["contains_adult_content"],
                    }

            save_state(cache_file_path, local_cache)

        except Exception as e:
            print(f"An error occurred: {e}")

        if mods_with_new_version:
            print("Updated mods:")
            print(tabulate(mods_with_new_version, headers="keys", tablefmt="pretty"))
            mods_with_new_version.clear()
        else:
            print("No updated mods found.")

        if loop:
            print(f"Sleeping for {frequency / 60 / 60} hour/s...")
            time.sleep(frequency)
        else:
            break


try:
    if __name__ == "__main__":
        parser = argparse.ArgumentParser()
        parser.add_argument("-k", "--api-key", required=True, help="API key for Nexus Mods")
        parser.add_argument("-g", "--game-name", required=True, help="Game domain name for Nexus Mods, eg. 'starfield'")
        parser.add_argument("-c", "--chat-id", required=True, help="Telegram chat ID")
        parser.add_argument("-t", "--tg-token", required=True, help="Telegram bot token")
        parser.add_argument("-o", "--topic-id", help="Telegram group topic ID", default="")
        parser.add_argument("-a", "--hide-adult-content", action="store_true", help="Hide adult content", default=False)
        parser.add_argument("-l", "--no-loop", action="store_true", help="Don't loot forever", default=False)
        parser.add_argument(
            "-f",
            "--frequency",
            help="Frequency of checks (defaults: 300s new mods, 3600s (1h) updates)",
            default=0,
            type=int,
        )

        sub_parser = parser.add_subparsers(dest="command")
        sub_parser.required = True

        additions_parser = sub_parser.add_parser("additions", help="Get latest additions")
        additions_parser.set_defaults(command="additions")

        updates_parser = sub_parser.add_parser("updates", help="Get latest updates")
        updates_parser.set_defaults(command="updates")

        args = parser.parse_args()

        match args.command:
            case "additions":
                additions(
                    api_key=args.api_key,
                    game_domain_name=args.game_name,
                    chat_id=args.chat_id,
                    tg_token=args.tg_token,
                    hide_adult_content=args.hide_adult_content,
                    loop=not args.no_loop,
                    topic_id=args.topic_id,
                    frequency=args.frequency or 300,
                )
            case "updates":
                updates(
                    api_key=args.api_key,
                    game_domain_name=args.game_name,
                    chat_id=args.chat_id,
                    tg_token=args.tg_token,
                    hide_adult_content=args.hide_adult_content,
                    loop=not args.no_loop,
                    topic_id=args.topic_id,
                    frequency=args.frequency or 3600,
                )
            case _:
                print("Invalid command")
except KeyboardInterrupt:
    print("\rExiting...")
    exit(0)
