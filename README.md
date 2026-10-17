# Morning Memo

A private podcast that writes and voices a five-minute briefing every morning.

```
5:45am  Claude scheduled task  ->  writes scripts/YYYY-MM-DD.md, pushes
        GitHub Action          ->  OpenRouter TTS -> site/<token>/episodes/*.mp3 + feed.xml
        GitHub Pages           ->  serves the feed
6:30am  Apple Podcasts / Overcast has the new episode
```

## Files
- `prompts/daily.md` - what the daily writer is told to do (edit this to change the content)
- `config.json` - TTS model and voice, feed title, how many episodes to keep
- `tools/memo.py` - renders audio, builds the feed and voice sampler
- `.github/workflows/render.yml` - runs on every pushed script, or manually

## One-time setup
1. Repo Settings > Secrets and variables > Actions > New secret: `llave` (your OpenRouter key).
2. Repo Settings > Pages > Source: **GitHub Actions**.
3. Any push renders the voice sampler (only new voices cost money). Open `<site_url>/<feed_path>/samples/` on your phone and pick a voice.
4. Put the winning `model` and `voice` into `config.json` under `tts`.
5. In Apple Podcasts: Library > ... > Follow a Show by URL > paste `<site_url>/<feed_path>/feed.xml`.
   (Overcast: + > Add URL.)

The feed path is a random token so the URL isn't guessable. Treat the feed URL like a password.

## Costs
TTS is billed per character by OpenRouter. A ~4,500 character memo is roughly $0.05-0.20 depending on the model.
GitHub Actions and Pages are free for this volume.

## Manual run
Push any `scripts/YYYY-MM-DD.md` and it renders. Or locally: `OPENROUTER_API_KEY=... python3 tools/memo.py episodes`.
