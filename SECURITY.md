# Security

Browsr uruchamia osobny profil Chromium przeznaczony do lokalnego developmentu.
Nie należy logować w nim prywatnych kont ani używać go jako codziennej przeglądarki.

Endpoint Chrome DevTools nasłuchuje wyłącznie na `127.0.0.1` i używa losowego
portu. Każdy proces działający jako ten sam użytkownik może jednak potencjalnie
odnaleźć port i sterować przeglądarką. Traktuj lokalne procesy i MCP podłączone
do Browsr jako zaufane.

Native messaging akceptuje tylko rozszerzenie Browsr o ID zapisanym w
`bridge.py`, a gniazdo Unix ma uprawnienia `0600`. Routing URL celowo dopuszcza
wyłącznie HTTP(S) do `localhost`, `127.0.0.1` i `[::1]`.

Problemy bezpieczeństwa zgłaszaj prywatnie właścicielowi repozytorium zamiast publikować działający exploit.
