# Changelog

## 0.1.0
- Первый релиз: userbot MAX (pymax) как аддон HAOS.
- Слушает все чаты (опция `ignore_chats`), конверты → вебхук.
- Резолвинг названий чатов (`fetch_chats`/`get_chat`) и имён участников
  (`sender`, `get_user`) с автосохранением в people.json/chats.json.
- Режим обучения: вопросы в чат утверждения, команды «ID X = Имя» / «ЧАТ X = Имя».
- Фото/файлы → base64 (PhotoAttachment.base_url / get_file_by_id).
- HTTP API: /health /auth /auth/status /auth/code /chats /names /send /learn.
- SMS-вход через веб-страницу /auth, сессия в /data/cache/main.db.
