# Browsr

Browsr łączy workspace Herdr z dedykowanym oknem Chromium. Każdy workspace ma
własną grupę kart, a przełączenie workspace w Herdr przełącza również aktywną
grupę w przeglądarce.

## Co robi

- utrzymuje jedną grupę kart Chromium na workspace Herdr;
- przekazuje kliknięte adresy `localhost`, `127.0.0.1` i `[::1]` do właściwej grupy;
- ponownie wykorzystuje istniejącą kartę podglądu zamiast mnożyć karty dla kolejnych portów;
- udostępnia temu samemu Chromium DevTools MCP dla Claude Code uruchomionego w Herdr;
- zachowuje osobny profil przeglądarki, niezależny od prywatnego Chrome.

## Codzienny workflow

1. W Herdr wybierz workspace.
2. Naciśnij `prefix+b` (w domyślnej konfiguracji: `Ctrl+Space`, potem `B`), aby pokazać Browsr.
3. Kliknij adres `http://localhost:PORT` obsługiwany przez Herdr albo pozwól agentowi otworzyć go przez `bridge.py open-url`.
4. Przełączaj workspace w Herdr. Browsr przełączy odpowiadającą mu grupę kart, nie odbierając fokusu terminalowi.

Profil Chromium i zapisane karty znajdują się w
`~/.local/share/herdr-dev-browser/chromium`. Nazwa katalogu jest historyczna i
celowo pozostaje niezmieniona, aby aktualizacje nie kasowały profilu.

## Instalacja

Wymagania: Linux, Herdr 0.8.2+, Chromium, Python 3.11+ i `npx` (tylko dla integracji z Chrome DevTools MCP).

```bash
herdr plugin link /home/g00rek/Projects/browsr
/home/g00rek/Projects/browsr/bridge.py setup
```

Dodaj akcję pluginu do `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+b"
type = "plugin_action"
command = "g00rek.browsr.launch"
description = "Show Browsr"
```

Integracja Claude Code korzysta z wrappera:

```bash
claude mcp add --scope user chrome-devtools \
  /home/g00rek/Projects/browsr/browsr-chrome-mcp.py
```

Skill dla Claude znajduje się w `skills/browsr/SKILL.md`. Można go skopiować do `~/.claude/skills/browsr/SKILL.md`.

## Polecenia

```bash
# pokaż lub uruchom dedykowane Chromium
herdr plugin action invoke launch --plugin g00rek.browsr

# lista kart przypisanych do bieżącego workspace (w pane Herdr)
./bridge.py workspace-tabs

# otwórz localhost bez odbierania fokusu agentowi
./bridge.py open-url http://localhost:3000

# adres losowego, lokalnego endpointu DevTools używanego przez MCP
./bridge.py mcp-endpoint
```

## Architektura

```text
Herdr events / localhost links
             |
          bridge.py ---- Unix socket ---- native_host.py
             |                               |
             |                         native messaging
             |                               |
             +------ dedicated Chromium + extension
                                      |
Claude Code -- browsr-chrome-mcp.py --+ (loopback DevTools)
```

Rozszerzenie jest ładowane jako unpacked extension z `extension/`. Native host
przekazuje komunikaty między Herdr a rozszerzeniem przez gniazdo dostępne tylko
lokalnie. Chromium wybiera losowy port DevTools na `127.0.0.1`; wrapper MCP
odczytuje go z profilu i nie uruchamia kolejnej przeglądarki.

Więcej informacji o modelu bezpieczeństwa: [SECURITY.md](SECURITY.md).

## Rozwój i testy

```bash
./scripts/check.sh
```

Po zmianie rozszerzenia zamknij dedykowane Chromium i uruchom je ponownie —
Chromium może zachować poprzedni service worker rozszerzenia do końca procesu.

## Licencja

[MIT](LICENSE)
