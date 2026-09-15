# Using an existing media folder layout

If you already have movies/TV organized on disk before setting this stack up
— with your own folder names, not this project's default `movies`/`tv`/
`downloads` — point `.env` straight at them instead of using the linker's
**Host storage** picker (that always creates fresh `movies`/`tv`/`downloads`
folders under whatever drive you choose, which won't match existing custom
names).

## Example

A real-world layout on a Linux NAS:

```
mark@nas:/mnt/Media$ pwd
/mnt/Media
mark@nas:/mnt/Media$ ls
movies  series
```

Edit `.env` and point the paths straight at the existing folders:

```bash
MEDIA_MOVIES_PATH=/mnt/Media/movies
MEDIA_TV_PATH=/mnt/Media/series
DOWNLOADS_PATH=/mnt/Media/downloads
```

(`downloads` doesn't need to exist yet — qBittorrent creates it. Put it
somewhere else, e.g. a separate drive, by giving `DOWNLOADS_PATH` a
different path entirely.)

Apply it by recreating just the affected containers:

```bash
sudo docker compose up -d sonarr radarr plex qbittorrent
```

Nothing changes inside the containers - Sonarr/Radarr/Plex/qBittorrent still
see `/tv`, `/movies`, `/downloads` internally; this only changes what those
paths point to on the host.

## Existing files aren't picked up automatically

Sonarr/Radarr won't retroactively notice movies/episodes that were already
sitting in those folders before you added the series/movie to them. For each
one you want it to recognize:

1. Add the series/movie in Sonarr/Radarr as usual (search by name)
2. Use **Manual Import** (or a library scan) so it matches the existing
   files on disk to the item you just added, instead of trying to
   re-download something you already have
