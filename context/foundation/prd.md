---
project: "bash-dash"
version: 1
status: draft
created: 2026-09-29
context_type: greenfield
product_type: web-app
target_scale:
  users: large  # ankieta: „do dziesięciu tysięcy”; idea.md szacuje dziesiątki do kilkuset graczy w trakcie wydarzenia
  # TODO: target_scale.qps — see Open Questions
  # TODO: target_scale.data_volume — see Open Questions
timeline_budget:
  mvp_weeks: 1
  hard_deadline: 2026-10-03
  after_hours_only: false
---

# bash-dash — PRD

## Vision & Problem Statement

Firma bierze udział w hackathonie jako partner i ma własne, raczej niszowe stoisko. Celem jest
zainteresowanie uczestników firmą i zachęcenie ich do aplikowania (rekrutacja). Spodziewany ruch
jest umiarkowany: dziesiątki, najwyżej kilkaset osób w ciągu wydarzenia. To pierwsze takie
wydarzenie, więc nie ma danych o status quo ani o jego koszcie (patrz Open Questions).

Insight:
- Wynik z rozwiązywania zadań w shellu pod presją czasu (5 minut) pokazuje realne umiejętności
  lepiej niż rozmowa przy stoisku.
- Dociekliwość też jest sygnałem. Kto znajdzie odpowiedzi w kodzie strony, wyciągnie je z binarki
  albo sprawnie użyje AI, pokazuje cechy, których szukamy. „Oszukiwanie” jest więc świadomie
  dozwolone.
- Ranking (Hall of fame) wyświetlany na ekranie przy stoisku buduje rywalizację i przyciąga ruch.

## User & Persona

**Persona główna: uczestnik hackathonu.** Developer lub student zajęty głównym zadaniem wydarzenia.
Przechodzi obok stoiska, skanuje kod QR i przez 5 minut rozwiązuje zadania z bash jako side-quest.
Na koniec dostaje wynik, miejsce w rankingu i 6-cyfrowy kod, z którym odbiera nagrodę na stoisku.

### Secondary persona
**Obsługa stoiska.** Weryfikuje wynik po 6-cyfrowym kodzie, wydaje nagrody, moderuje nicki
w Hall of fame. Firma (rekrutacja) jest beneficjentem, a nie bezpośrednim użytkownikiem.

## Success Criteria

### Primary
- Gra działa bez awarii przez całe wydarzenie (2026-10-03). Każdy, kto zeskanował QR, może
  rozegrać sesję, a każdy wydany 6-cyfrowy kod da się zweryfikować na stoisku.

### Secondary
- Hall of fame na ekranie przy stoisku przyciąga ruch.

### Guardrails
- Ranking i kody przetrwają awarię i odtworzenie serwisu.
- Żaden gracz nie może swoją komendą (np. fork bombą czy zajęciem CPU) zablokować gry innym ani
  zepsuć serwisu.
- Gracz nie może samodzielnie wydłużyć ani zresetować czasu (np. odświeżeniem strony czy zmianą
  zegara w telefonie). Każda poprawna odpowiedź dodaje ustawiany przez obsługę bonus czasu,
  domyślnie 15 sekund, a komendy wysłane po czasie się nie liczą.

## User Stories

### US-01: Uczestnik rozgrywa sesję i odbiera nagrodę

- **Given** uczestnik stoi przy stoisku z telefonem, a na ekranie widać ranking z aktualnym kodem QR
- **When** skanuje kod, podaje nick, klika „Start” i przez 5 minut rozwiązuje zadania, wpisując komendy
- **Then** widzi podsumowanie z liczbą rozwiązanych zadań, liczbą podejść, miejscem w rankingu
  i 6-cyfrowym kodem, a obsługa znajduje ten kod w panelu i widzi ten sam wynik

#### Acceptance Criteria
- Przed startem gracz widzi zasady: 5 minut, zadania po kolei, każda komenda to podejście, mniej
  podejść jest lepiej.
- Każda wysłana komenda zwiększa licznik podejść, także komenda rozpoznawcza (`ls`, `cat`).
- Gracz nie przejdzie do następnego zadania, dopóki nie rozwiąże bieżącego.
- Odświeżenie strony w trakcie gry wraca do tej samej sesji z tym samym pozostałym czasem.
- Komenda wysłana po upływie 5 minut nie jest liczona.
- Po zakończeniu gry wynik gracza pojawia się w Hall of fame (o ile mieści się w top N i nick nie
  jest ukryty).
- Skan kodu QR starszego niż ustawiony czas ważności (domyślnie ok. 15 minut) nie wystawia
  indywidualnego kodu gry.

## Functional Requirements

Numeracja jest stabilna. FR-009, FR-014 i FR-015 usunięto w rundzie sokratejskiej (patrz niżej).

### Wejście
- FR-001: Gracz może otrzymać indywidualny kod gry tylko z ważnym tokenem z kodu QR wyświetlanego przy stoisku. Czas ważności tokenu (domyślnie ok. 15 min) ustawia obsługa, a wartość 0 oznacza, że token nie wygasa. Priority: must-have
  > Socrates: Kontrargument: „ekran przy stoisku to punkt awarii; gdy padnie, nikt nie zacznie
  > gry”. Rozstrzygnięcie: konfigurowalny czas ważności. Ustawienie 0 wyłącza wygasanie
  > i działa jako fallback.
- FR-002: Gracz może przeczytać zasady i podać nick przed startem. Priority: must-have
  > Socrates: Kontrargument: „obraźliwe nicki trafiają prosto na ekran”. Rozstrzygnięcie:
  > wystarczy moderacja reaktywna (FR-012), bez filtra ani akceptacji z góry.
- FR-019: Po wejściu z QR gracz otrzymuje trwały, jednorazowy kod gry, który może wpisać na
  komputerze. Pierwszy Start na telefonie albo komputerze atomowo zużywa kod i tworzy jedną
  sesję; telefon pozostaje podglądem i po zakończeniu pokazuje podsumowanie. Priority: must-have

### Rozgrywka
- FR-003: Gracz może rozwiązywać zadania z zestawu głównego w stałej kolejności, bez pomijania. Priority: must-have
  > Socrates: Rozważono „utknięcie na jednym zadaniu” i „niepewną kolejność trudności”.
  > Brak kontrargumentu; FR stoi jak jest.
- FR-004: Gracz może wpisać komendę bash i zobaczyć jej wyjście oraz informację, czy rozwiązała zadanie. W zadaniach z losowo zmienianymi danymi lub dodatkowym sprawdzeniem weryfikacja odrzuca samo wypisanie oczekiwanego wyniku. Priority: must-have
  > Socrates: Kontrargument: „opóźnienie ok. 1 s na komendę przy tłoku zjada 5-minutowy
  > limit”. Rozstrzygnięcie: ryzyko akceptowane przy spodziewanym ruchu.
- FR-005: Gracz może wysłać dowolną liczbę komend. Każda liczy się jako podejście. Priority: must-have
  > Socrates: Kontrargument: „podejścia to tylko tie-breaker; może nie warto tłumaczyć tej
  > zasady”. Rozstrzygnięcie: zostaje, bo rozstrzyga remisy. Zasada jest jasno opisana na ekranie
  > startowym.
- FR-006: Gracz może widzieć pozostały czas i po odświeżeniu strony kontynuować tę samą sesję bez resetu czasu. Priority: must-have
  > Socrates: Kontrargument: „zamknięcie karty lub zmiana przeglądarki gubi sesję”.
  > Rozstrzygnięcie: akceptujemy. Wznowienie działa tylko w tej samej przeglądarce.
- FR-007: Gracz może grać do upływu 5 minut albo do rozwiązania wszystkich zadań. Wtedy gra się kończy. Priority: must-have
  > Socrates: Rozważono „5 minut to za mało” i „pisanie na telefonie”. Brak kontrargumentu;
  > FR stoi jak jest.
- FR-018: Po każdej poprawnej odpowiedzi serwer wydłuża pozostały czas o bonus ustawiany przez
  obsługę (domyślnie 15 sekund; 0 wyłącza bonus). Priority: must-have

### Podsumowanie
- FR-008: Gracz może zobaczyć liczbę rozwiązanych zadań, liczbę podejść, miejsce w rankingu i unikalny 6-cyfrowy kod. Priority: must-have
  > Socrates: Rozważono „zgubienie kodu” i „zmienność miejsca”. Brak kontrargumentu;
  > FR stoi jak jest.

### Hall of fame
- FR-010: Obsługa może wyświetlić automatycznie odświeżany ranking top N (miejsce, nick, liczba zadań, liczba podejść) bez ukrytych nicków, a obok listę ostatnio zakończonych gier. Priority: must-have
  > Socrates: Kontrargument: „top N zniechęca słabszych graczy, bo nie widzą się na ekranie”.
  > Rozstrzygnięcie: dodajemy na ekranie „ostatnie wyniki”, żeby każdy przez chwilę widział
  > swój wynik.
- FR-017: Obsługa może wyświetlić na ekranie rankingu aktualny, rotujący kod QR z tokenem startowym. Priority: must-have
  > Socrates: Rozważono „konflikt układu QR z rankingiem” i „skan z daleka”. Brak
  > kontrargumentu; FR stoi jak jest.

### Obsługa stoiska
- FR-011: Obsługa może wyszukać wynik po 6-cyfrowym kodzie (nick, liczba zadań, podejścia, miejsce, czas). Priority: must-have
  > Socrates: Kontrargument: „kolizja albo podejrzenie cudzego kodu”. Rozstrzygnięcie: kod jest
  > gwarantowanie unikalny, a przy odbiorze nagrody obsługa pyta też o nick.
- FR-012: Obsługa może ukryć nick z Hall of fame bez kasowania wyniku. Ukrycie działa jak dyskwalifikacja. Priority: must-have
  > Socrates: Kontrargument: „podmiana nicku jest łagodniejsza niż ukrycie”. Rozstrzygnięcie:
  > kto trolluje obraźliwym nickiem, ten się dyskwalifikuje. Ukrycie zostaje.
- FR-013: Obsługa może oznaczyć „nagroda wydana”. Priority: must-have
  > Socrates: Kontrargument: „bez tego ta sama osoba odbierze nagrodę dwa razy”. Rozstrzygnięcie:
  > podniesione z nice-to-have do must-have.

### Inne
- FR-016: Gracz nie może (miękko) rozpocząć drugiej gry z tej samej przeglądarki. Priority: nice-to-have
  > Socrates: Kontrargument: „powtórki zawyżają ranking, a blokada w przeglądarce ich nie
  > zatrzyma”. Rozstrzygnięcie: zostaje nice-to-have. Duplikaty w czołówce obsługa ukrywa
  > (FR-012), a nagroda przysługuje raz na osobę (FR-013).

### Usunięte w rundzie sokratejskiej
- ~~FR-009: CTA rekrutacyjne~~. Usunięte: ludzie i tak podchodzą do stoiska po nagrody, więc
  rozmowa rekrutacyjna odbywa się tam.
- ~~FR-014: korekta lub unieważnienie wyniku~~. Usunięte: dyskwalifikację załatwia FR-012.
  Wyjątkowe korekty robi ręcznie operator systemu, poza panelem obsługi.
- ~~FR-015: ukryta nagroda (odpowiedzi celowo w danych strony)~~. Usunięte: nie umieszczamy
  odpowiedzi celowo. Kto „schakuje” system, zasługuje na 42/42.

## Non-Functional Requirements

- Awaria i odtworzenie serwisu traci wyniki i kody z najwyżej ostatnich ok. 5 minut.
- Komenda jednego gracza (np. fork bomba, pętla, zajęcie CPU lub pamięci) nie wpływa na
  możliwość gry innych ani na dostępność serwisu. Każda komenda kończy się (wynikiem lub
  przekroczeniem czasu) w ≤ ok. 6 s.
- Pełną rozgrywkę da się przejść w aktualnej mobilnej przeglądarce (Chrome na Androidzie, Safari
  na iOS) na danych komórkowych, bez zależności od Wi-Fi organizatora.

## Business Logic

Gracze są szeregowani według liczby rozwiązanych zadań (malejąco), przy remisie według liczby
wysłanych komend (rosnąco), a dalej według czasu od startu sesji do ostatniego poprawnego
rozwiązania (rosnąco).

Wejścia: sekwencja komend wysłanych przez gracza w ciągu 5 minut od startu sesji (liczonych od
momentu kliknięcia „Start”, niezależnie od urządzenia gracza) oraz wynik weryfikacji każdej z nich. Zadanie uznaje się za rozwiązane tylko wtedy, gdy
komenda daje oczekiwany wynik, a w zadaniach z losowo zmienianymi danymi także po ponownym
wykonaniu na zmienionych danych. W tych zadaniach samo wypisanie oczekiwanego wyniku nie
przechodzi; w zadaniach o stałym wyniku jest to akceptowane (brak anti-cheatu, zob. Non-Goals). Komendy wysłane po czasie nie wpływają na
wynik.

Wyjście: miejsce w rankingu. Gracz widzi je na ekranie podsumowania, a obsługa na ekranie przy
stoisku. Nicki ukryte przez obsługę (dyskwalifikacja) nie występują w rankingu. Czas ostatniego
rozwiązania służy wyłącznie do rozstrzygania remisów i **nie jest pokazywany** na ekranie Hall of
fame, żeby go nie zaciemniać. Nie ma punktów za trudność zadania.

## Access Control

Trzy role, bez kont użytkowników:

| Rola | Jak wchodzi | Co może |
|---|---|---|
| **Gracz** (uczestnik) | Skanuje kod QR wyświetlany na ekranie przy stoisku. Adres zawiera krótko ważny token (ok. 15 min), który wystawia trwały 5-znakowy kod gry. Podaje nick (wymagany, nieunikalny). E-maila nie zbieramy. | Rozpocząć sesję na telefonie albo przenieść Start na komputer, wysyłać komendy z urządzenia, które rozpoczęło grę, zobaczyć podsumowanie i swój 6-cyfrowy kod. |
| **Obsługa stoiska** | Logowanie hasłem. | Oglądać Hall of fame razem z aktualnym kodem QR (widok na ekran przy stoisku), ustawiać czas ważności tokenu i bonus czasu za poprawną odpowiedź, wyszukiwać po kodzie, ukrywać nicki (dyskwalifikacja), oznaczać wydanie nagrody. |

- Nie ma publicznego rankingu. Hall of fame razem z QR widzi tylko zalogowana obsługa, która
  wyświetla go na ekranie przy stoisku.
- Token w kodzie QR pozwala przez ok. 15 minut **pobrać indywidualny kod gry**. Kod jest wydawany
  osobom fizycznie przy stoisku, ale sama gra może rozpocząć się później, także poza stoiskiem.
  Sesja zaczyna swój limit czasu dopiero po kliknięciu Start.
- Indywidualny kod gry nie wygasa. Składa się z 5 znaków, nie rozróżnia wielkości liter i pomija
  znaki mylące się wizualnie (`O/0`, `I/1/L`, `B/8`, `G/6`, `S/5`, `Z/2`). Raz użyta wartość
  nigdy nie wraca do puli.
- Wejście z wygasłym tokenem albo bez tokenu kończy się odmową z informacją „zeskanuj kod przy
  stoisku”, a nie ekranem startowym.
- Identyfikacja gracza przy odbiorze nagrody odbywa się wyłącznie po 6-cyfrowym kodzie.
  Nick nie jest identyfikatorem.
- Jedno podejście na osobę: miękka blokada w przeglądarce (nice-to-have). Obejście przez tryb
  incognito jest akceptowane.
- Dane osobowe: zbieramy tylko nick, więc nie potrzeba procedur RODO.

## Non-Goals

Funkcjonalne:
- **Brak CTA rekrutacyjnego w grze.** Ludzie i tak podchodzą do stoiska po nagrody, a rozmowa
  rekrutacyjna odbywa się tam.
- **Brak walki z oszukiwaniem.** Nie ma anti-cheatu ani wykrywania AI. Wyciąganie odpowiedzi
  z systemu (np. `strings` na binarce) i sprawne użycie AI są dozwolone i traktowane jako sygnał
  dociekliwości.
- **Brak celowo ukrytych odpowiedzi w stronie.** Nie umieszczamy ich specjalnie. Kto „schakuje”
  system, zasługuje na 42/42.
- **Brak podpowiedzi i rozwiązań w UI.** Nie ma „show solution”, rozwiązań innych graczy ani
  linku do repozytorium z odpowiedziami. Na stronie nie może być żadnego linku prowadzącego do
  odpowiedzi.
- **Tylko zestaw główny zadań.** Bez `12days` i `oops` oraz bez własnych, nowych zadań. Zadania,
  które nie działają w bezpiecznym środowisku uruchamiania komend, wycinamy, a nie naprawiamy.
- **Jedno wydarzenie, bez kont i historii graczy.** Nie obsługujemy wielu wydarzeń, kont, profili
  ani historii gier ponad jedną sesję.
- **Brak publicznego rankingu.** Hall of fame widzi tylko obsługa i ekran przy stoisku.
- **Brak zbierania e-maili i innych danych osobowych.** Zbieramy tylko nick.
- **Brak ręcznej korekty lub unieważniania wyniku w panelu.** Dyskwalifikacja odbywa się przez
  ukrycie nicku, a wyjątkowe korekty robi ręcznie operator systemu, poza panelem obsługi.
- **Brak punktów za trudność zadania i pomijania zadań.**

Niefunkcjonalne:
- **Brak gwarancji wydajności przy tłoku.** Opóźnienie ok. 1 s na komendę przy wielu
  równoczesnych graczach jest akceptowane.
- **Brak twardej blokady powtórnej gry.** Blokada w przeglądarce jest tylko miękka
  (nice-to-have), a obejście przez incognito jest akceptowane.

## Open Questions

1. **Jakie jest status quo i jego koszt?** Pierwsze takie wydarzenie, brak danych o tym, jak
   stoisko radzi sobie bez gry. Właściciel: użytkownik.
2. **Jakie są nagrody?** Czy zależą od miejsca lub wyniku, czy są za sam udział? Właściciel:
   użytkownik. Termin: przed 2026-10-03.
3. **Rozmiar top N i liczba „ostatnich wyników” na ekranie, częstotliwość odświeżania.**
   Właściciel: użytkownik. Termin: w trakcie implementacji.
4. **Które z 42 zadań wyciąć?** Zależy od testu wzorcowych rozwiązań pod utwardzeniem.
   Właściciel: użytkownik. Termin: przed 2026-10-03.
5. **Hosting VM i domena.** Właściciel: użytkownik. Termin: przed 2026-10-03. Blokuje wdrożenie.
6. **Domyślny czas ważności tokenu QR i częstotliwość jego rotacji.** Przyjęto ok. 15 minut,
   z możliwością zmiany przez obsługę. Właściciel: użytkownik.
7. **Jaki jest spodziewany szczytowy ruch (`target_scale.qps`)?** Brak w notatkach. Właściciel:
   użytkownik.
8. **Jaka jest spodziewana skala danych (`target_scale.data_volume`)?** Brak w notatkach.
   Właściciel: użytkownik.
