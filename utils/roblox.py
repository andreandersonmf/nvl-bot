import aiohttp
from urllib.parse import quote


def get_member_roblox_username(member) -> str:
    username = member.nick or member.display_name or member.name
    return username.strip()


async def username_to_user_data(username: str):
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {
        "usernames": [username],
        "excludeBannedUsers": False
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            entries = data.get("data", [])
            if not entries:
                return None

            user = entries[0]
            return {
                "id": user["id"],
                "username": user["name"],
                "display_name": user.get("displayName", user["name"])
            }


async def get_avatar_url(user_id: int):
    url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=150x150&format=Png&isCircular=false"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None

            data = await resp.json()
            items = data.get("data", [])
            if not items:
                return None

            return items[0].get("imageUrl")


async def get_profile_data_from_member(member):
    username = get_member_roblox_username(member)
    resolved = await username_to_user_data(username)

    if not resolved:
        return {
            "username": username,
            "user_id": None,
            "avatar_url": None,
            "profile_url": f"https://www.roblox.com/search/users?keyword={quote(username)}"
        }

    avatar_url = await get_avatar_url(resolved["id"])
    profile_url = f"https://www.roblox.com/users/{resolved['id']}/profile"

    return {
        "username": resolved["username"],
        "user_id": resolved["id"],
        "avatar_url": avatar_url,
        "profile_url": profile_url
    }