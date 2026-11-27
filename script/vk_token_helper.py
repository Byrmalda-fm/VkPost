"""Помощник получения пользовательского токена VK (Implicit Flow, Standalone-приложение).

Использование:
    python scripts/vk_token_helper.py APP_ID
Откройте выведенную ссылку ПОД ТЕМ АККАУНТОМ, который будет отправлять предложенные записи
(не руководителем сообщества!), разрешите доступ, затем вставьте сюда адрес страницы, на которую
вас перенаправит VK (в адресной строке будет #access_token=...). Токен — секрет: никому не показывайте.
"""
import sys
from urllib.parse import parse_qs, urlencode, urlparse


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("Использование: python scripts/vk_token_helper.py APP_ID")
    params = {
        "client_id": sys.argv[1],
        "redirect_uri": "https://oauth.vk.com/blank.html",
        "display": "page",
        "scope": "wall,photos,offline",
        "response_type": "token",
        "v": "5.199",
    }
    print("1) Откройте ссылку в браузере (аккаунт — обычный участник, не руководитель):\n")
    print("https://oauth.vk.com/authorize?" + urlencode(params))
    print("\n2) Вставьте сюда адрес, на который вас перенаправило:")
    url = input("> ").strip()
    frag = parse_qs(urlparse(url).fragment)
    token = (frag.get("access_token") or [None])[0]
    if not token:
        err = frag.get("error_description") or parse_qs(urlparse(url).query).get("error_description")
        sys.exit(f"Токен не найден в адресе. {err[0] if err else ''}")
    expires = (frag.get("expires_in") or ["?"])[0]
    print(f"\nVK_USER_TOKEN={token}")
    print(f"Срок действия (сек.): {expires} (0 = бессрочный). Запишите токен в .env.")


if __name__ == "__main__":
    main()
