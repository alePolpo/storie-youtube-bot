# Storie YouTube + Amazon

Bot Telegram. Gli mandi:

- un link YouTube: risponde con due PNG, la storia 1080x1920 (card con copertina,
  durata, avatar, titolo e canale su sfondo coi colori della copertina) e la sola
  card con gli angoli trasparenti;
- un link Amazon: risponde con il link affiliato pulito (`/dp/ASIN?tag=...`).

## Configurazione

`.env` (vedi `.env.example`):

- `TELEGRAM_BOT_TOKEN`: token da @BotFather
- `TELEGRAM_CHAT_ID`: l'unica chat a cui risponde (`python3 bot.py --chat-id`)
- `AFFILIATE_TAG`: tag affiliato Amazon

Su GitHub Actions gli stessi valori vanno nei secrets: `gh secret set -f .env`.

## Prova locale

    pip install pillow
    python3 bot.py --test https://youtu.be/<id>   # scrive storia.png e copertina.png
    python3 bot.py                                # avvia il bot

Font: Inter (SIL Open Font License, vedi fonts/LICENSE.txt).
