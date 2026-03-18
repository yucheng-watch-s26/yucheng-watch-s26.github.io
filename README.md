# YC Watch S26 — протокол передачи витальных показателей по BLE

Реверс-инжиниринг на основе декомпилированного Android SDK (`dump/sources/com/yucheng/ycbtsdk/`).

---

## BLE-сервисы

Часы предоставляют три кастомных сервиса ([SERVICES.txt](SERVICES.txt)):

| UUID сервиса | Роль |
|---|---|
| `be940000-…` | **YC custom** — основной канал данных |
| `6e400001-…` | Nordic UART — резервный (те же YC-фреймы) |
| `0000ae00-…` | JL chip (JieLi RCSP) — другой SDK, несовместим |

### Характеристики YC custom сервиса

| UUID | Свойства | Назначение |
|---|---|---|
| `be940001` | write + indicate | Команды приложение → часы; ответы часы → приложение |
| `be940002` | write only | Вторичная запись (например, ECG групповые фреймы) |
| `be940003` | indicate only | Дополнительные нотификации часы → приложение |

Обнаружение сервисов/характеристик происходит в
[BleHelper.java:915–1173](dump/sources/com/yucheng/ycbtsdk/gatt/BleHelper.java).
SDK перебирает все GATT-сервисы и сопоставляет UUID в следующем приоритете:
1. `be940000` → использовать `be940001` (write), `be940003` (indicate)
2. `6e400001` → использовать `6e400002` (write), `6e400003` (notify)
3. `0000ae00` → путь JL chip (протокол RCSP, вне области применения)

---

## Формат фрейма

Источник: [YCBTClientImpl.java:3778–3810](dump/sources/com/yucheng/ycbtsdk/core/YCBTClientImpl.java)

```
[group(1), key(1), total_len_lo(1), total_len_hi(1), payload(N), crc_lo(1), crc_hi(1)]
```

- `group` = `dataType >> 8`
- `key` = `dataType & 0xFF`
- `total_len` = `payload_len + 6` (все байты включая заголовок и CRC)
- `crc16` считается по байтам `[0 .. total_len-3]` (всё кроме последних 2 байт)

### CRC16

Источник: [ByteUtil.java:107–116](dump/sources/com/yucheng/ycbtsdk/utils/ByteUtil.java)

```python
def crc16(data: bytes) -> int:
    s = 0xFFFF
    for b in data:
        s = (((s << 8) & 0xFF00) | ((s >> 8) & 0xFF)) ^ (b & 0xFF)
        s ^= (s & 0xFF) >> 4
        s ^= (s << 12) & 0xFFFF
        s ^= ((s & 0xFF) << 5) & 0xFFFF
    return s & 0xFFFF
```

---

## Кодирование dataType

`dataType = (group << 8) | key`

Все константы находятся в [Constants.java](dump/sources/com/yucheng/ycbtsdk/Constants.java)
(внутренний класс `DATATYPE`) и [CMD.java](dump/sources/com/yucheng/ycbtsdk/core/CMD.java)
(внутренние классы `KEY_AppControl`, `KEY_Real`, `KEY_Health`, `KEY_Get`).

---

## Команды приложение → часы (витальные показатели)

### Переключатели режима замера

| Константа | dataType (hex) | group/key | Payload | Эффект |
|---|---|---|---|---|
| `AppHeartSwitch` | `0x0301` | 3/1 | `[0x01]` / `[0x00]` | Мониторинг пульса вкл/выкл |
| `AppBloodSwitch` | `0x0302` | 3/2 | `[0x01]` / `[0x00]` | Мониторинг давления вкл/выкл |
| `AppControlReal` | `0x0309` | 3/9 | `[0x01]` / `[0x00]` | Стрим реального времени вкл/выкл |

Источник: [CMD.java:77,87,101](dump/sources/com/yucheng/ycbtsdk/core/CMD.java) —
`HeartTest=1`, `BloodTest=2`, `RealData=9` в классе `KEY_AppControl`.

### Одиночные триггеры замера

#### AppStartMeasurement — `0x032F`

Источник: [YCBTClient.java:749–751](dump/sources/com/yucheng/ycbtsdk/YCBTClient.java)

```java
YCBTClientImpl.sendSingleData2Device(AppStartMeasurement, new byte[]{(byte)start, (byte)type}, ...)
```

Payload: `[start(1), measureType(1)]`
- `start=1` → начать, `start=0` → остановить
- `type=1` → замер давления

Используется в [BloodPressureMeasureActivity.java:205](dump/sources/com/yucheng/smarthealthpro/home/activity/bloodpressure/activity/BloodPressureMeasureActivity.java):
```java
YCBTClient.appStartMeasurement(1, 1, callback)  // запустить замер давления
```

#### AppStartBloodMeasurement — `0x032E`

Источник: [YCBTClient.java:745–747](dump/sources/com/yucheng/ycbtsdk/YCBTClient.java)

```java
YCBTClientImpl.sendSingleData2Device(AppStartBloodMeasurement,
    new byte[]{start, sbp_ref, dbp_ref, heart_ref, height_cm, weight_kg, age, sex}, ...)
```

Payload (8 байт):
| Байт | Поле | Пример |
|---|---|---|
| 0 | start (1=вкл, 0=выкл) | 1 |
| 1 | референсное SAД | 115 |
| 2 | референсное ДАД | 80 |
| 3 | референсный пульс | 70 |
| 4 | рост (см) | 170 |
| 5 | вес (кг) | 70 |
| 6 | возраст | 30 |
| 7 | пол (0=муж, 1=жен) | 0 |

Используется в [RealBloodPressureMeasureActivity.java](dump/sources/com/yucheng/smarthealthpro/settings/uploadnativedata/RealBloodPressureMeasureActivity.java).
Результат возвращается как `RT_BPDONE (0x0410)`.

### Синхронизация времени — `0x0100`

```python
epoch_yc = int(time.time()) - 946684800   # секунды с 2000-01-01
payload = struct.pack("<I", epoch_yc)      # 4 байта little-endian
```

Источник: [YCBTClient.java:66](dump/sources/com/yucheng/ycbtsdk/YCBTClient.java) —
`SecFrom30Year = 946684800`.

### Команды запроса истории (poll)

Источник: [YCBTClient.java:1435–1437](dump/sources/com/yucheng/ycbtsdk/YCBTClient.java)

```java
YCBTClientImpl.sendDataType2Device(dataType, groupType=3, new byte[0], priority=2, cb)
```

Все команды истории используют **пустой payload**:

| Константа | dataType (hex) | dataType ответа | Описание |
|---|---|---|---|
| `Health_HistoryBlood` | `0x0508` | `0x0517` | Сохранённые записи давления |
| `Health_HistoryHeart` | `0x0506` | `0x0515` | Сохранённые записи пульса |
| `Health_HistoryAll` | `0x0509` | `0x0518` | Сохранённые записи всех показателей |
| `Health_HistoryBloodOxygen` | `0x051A` | — | Сохранённые записи SpO2 |
| `Health_HistoryComprehensiveMeasureData` | `0x052F` | — | Сохранённые комплексные записи |
| `GetAllRealDataFromDevice` | `0x0220` | — | Кэшированный снимок в реальном времени |
| `GetRealBloodOxygen` | `0x0211` | — | Текущий SpO2 |

---

## Ответы часы → приложение

Парсинг в [DataUnpack.java](dump/sources/com/yucheng/ycbtsdk/core/DataUnpack.java),
диспетчеризация в [YCBTClientImpl.java:2716+](dump/sources/com/yucheng/ycbtsdk/core/YCBTClientImpl.java).

### Стрим реального времени (group=6)

#### `RT_HR` — `0x0601`

Источник: [DataUnpack.java:7281–7287](dump/sources/com/yucheng/ycbtsdk/core/DataUnpack.java)

| Байт | Поле |
|---|---|
| 0 | Пульс (уд/мин) |

#### `RT_BLOOD` — `0x0603`

Источник: [DataUnpack.java:7166–7194](dump/sources/com/yucheng/ycbtsdk/core/DataUnpack.java)

```java
b2 = bArr[0] & 0xFF  // САД
b3 = bArr[1] & 0xFF  // ДАД
b4 = bArr[2] & 0xFF  // Пульс
```

| Байт | Поле |
|---|---|
| 0 | САД (мм рт. ст.) |
| 1 | ДАД (мм рт. ст.) |
| 2 | Пульс (уд/мин) |
| 3 (опц.) | HRV |
| 4 (опц.) | SpO2 (%) |
| 5–6 (опц.) | Температура: целая часть + дробная |

#### `RT_SPO2` — `0x0602`

Источник: [DataUnpack.java:7196–7202](dump/sources/com/yucheng/ycbtsdk/core/DataUnpack.java)

| Байт | Поле |
|---|---|
| 0 | SpO2 (%) |

#### `RT_COMP` — `0x060A` (Комплексный)

Источник: [DataUnpack.java:7204–7237](dump/sources/com/yucheng/ycbtsdk/core/DataUnpack.java)

Минимум 20 байт:

| Смещение | Поле |
|---|---|
| 0–2 | Шаги (24-bit LE) |
| 3–4 | Дистанция (16-bit LE, м) |
| 5–6 | Калории (16-bit LE) |
| 7 | Пульс (уд/мин) |
| 8 | САД (мм рт. ст.) |
| 9 | ДАД (мм рт. ст.) |
| 10 | SpO2 (%) |
| 11 | Частота дыхания |
| 12 | Температура, целая часть |
| 13 | Температура, дробная часть |
| 14 | Статус ношения |
| 15 | Заряд батареи (%) |
| 16–19 | PPI (32-bit LE) |
| 20 | Сахар крови |
| 21–22 | Мочевая кислота (16-bit LE) |
| 23 | Липиды крови, целая часть |
| 24 | Липиды крови, дробная часть |

### Завершение замера (group=4)

#### `RT_DONE` — `0x040E` (DeviceMeasurementResult)

Источник: [YCBTClientImpl.java:1908–1910](dump/sources/com/yucheng/ycbtsdk/core/YCBTClientImpl.java)

| Байт | Поле |
|---|---|
| 0 | Тип замера |
| 1 | Результат: `1`=OK, `2`=FAIL, `3`=CANCEL |

Приходит после завершения `AppStartMeasurement (0x032F)`. При OK приложение
вызывает `healthHistoryData(Health_HistoryBlood)` для получения сохранённого результата.
Источник: [BloodPressureMeasureActivity.java:113–134](dump/sources/com/yucheng/smarthealthpro/home/activity/bloodpressure/activity/BloodPressureMeasureActivity.java)

#### `RT_BPDONE` — `0x0410` (DeviceInflatedBloodMeasureResult)

Источник: [YCBTClientImpl.java:1914–1916](dump/sources/com/yucheng/ycbtsdk/core/YCBTClientImpl.java)

| Байт | Поле |
|---|---|
| 0 | Статус (`0`=OK) |
| 1 | САД (мм рт. ст.) |
| 2 | ДАД (мм рт. ст.) |

Приходит после завершения `AppStartBloodMeasurement (0x032E)`.
Результат встроен в payload — отдельный запрос истории не нужен.

---

## Форматы записей истории

Устройство отвечает на команды запроса истории последовательностью фреймов:

1. **Заголовочный фрейм** (тот же key что в запросе): содержит количество записей как `uint16_le` по смещению 0
2. **Фреймы с данными** (key = key_запроса + `0x0F`): упакованные записи без разделителей
3. **Маркер конца** (key = `0x80`): передача завершена

### Записи давления — `0x0517` (8 байт каждая)

| Смещение | Размер | Поле |
|---|---|---|
| 0 | 4 | Временная метка (эпоха YC, uint32 LE) |
| 4 | 1 | Статус |
| 5 | 1 | САД (мм рт. ст.) |
| 6 | 1 | ДАД (мм рт. ст.) |
| 7 | 1 | Пульс (уд/мин) |

### Записи пульса — `0x0515` (6 байт каждая)

| Смещение | Размер | Поле |
|---|---|---|
| 0 | 4 | Временная метка (эпоха YC, uint32 LE) |
| 4 | 1 | Статус |
| 5 | 1 | Пульс (уд/мин) |

### Записи всех показателей — `0x0518` (20 байт каждая)

| Смещение | Размер | Поле |
|---|---|---|
| 0 | 4 | Временная метка (эпоха YC, uint32 LE) |
| 4–5 | 2 | (зарезервировано) |
| 6 | 1 | Пульс (уд/мин) |
| 7 | 1 | САД (мм рт. ст.) |
| 8 | 1 | ДАД (мм рт. ст.) |
| 9 | 1 | SpO2 (%) |
| 10 | 1 | (зарезервировано) |
| 11 | 1 | Температура |

### Конвертация временной метки

```python
unix_ts = ts_yc + 946684800   # смещение эпохи YC = 2000-01-01 00:00:00 UTC
```

Источник: [YCBTClient.java:66](dump/sources/com/yucheng/ycbtsdk/YCBTClient.java) —
`SecFrom30Year = 946684800`.

---

## Два режима замера давления

### Режим А — активный замер (через `0x032E`)

1. Приложение отправляет `AppStartBloodMeasurement (0x032E)` с биометрическими референсными значениями
2. Часы измеряют, стримят промежуточные значения через `RT_BLOOD (0x0603)`
3. Часы отправляют `RT_BPDONE (0x0410)` с итоговыми САД/ДАД по завершении
4. **Отдельный запрос истории не нужен** — результат в payload `0x0410`

### Режим Б — одиночный замер + запрос истории (через `0x032F` + poll `0x0508`)

1. Приложение отправляет `AppStartMeasurement (0x032F)` с payload `[1, 1]`
2. Часы измеряют (промежуточный стрим отсутствует)
3. Часы отправляют `RT_DONE (0x040E)` со статусом
4. При OK: приложение вызывает `healthHistoryData(Health_HistoryBlood = 0x0508)`
5. Часы отдают сохранённые записи как фреймы `0x0517`

Источник: [BloodPressureMeasureActivity.java:164](dump/sources/com/yucheng/smarthealthpro/home/activity/bloodpressure/activity/BloodPressureMeasureActivity.java):
```java
DataSyncUtils.getWatchesData(Constants.DATATYPE.Health_HistoryBlood)
```

---

## GATT HR (стандартный Bluetooth)

Часы также предоставляют стандартный GATT HR сервис `0x180D`, характеристика `0x2A37`
([SERVICES.txt](SERVICES.txt)). Это пассивная нотификация, которая приходит непрерывно
без каких-либо write-команд. Формат: стандартное GATT HR измерение (байт флагов + BPM).

Этот канал независим от YC custom протокола.

---

## Валидация давления на стороне приложения

Источник: [BloodPressureMeasureActivity.java:150](dump/sources/com/yucheng/smarthealthpro/home/activity/bloodpressure/activity/BloodPressureMeasureActivity.java)

Приложение фильтрует отображаемые значения:
- САД: 60–250 мм рт. ст.
- ДАД: 30–160 мм рт. ст.
- Ни одно значение не может быть `0` или `"00"`

---

## Цикл опроса — почему нельзя просто подписаться и ждать

### Проблема: часы засыпают и шлют 0

Если после отправки команды включения (`0x0301 HEART_ON`, `0x0302 BLOOD_ON`,
`0x0309 REAL_STREAM`) больше ничего не писать в характеристику,
часы через ~30–60 секунд прекращают замер и начинают присылать `RT_HR = 0 BPM`.

Причина: команды `AppHeartSwitch` / `AppBloodSwitch` / `AppControlReal` —
это **переключатели режима**, а не постоянные подписки. Устройство
трактует тишину на входе как завершение сессии и останавливает оптический сенсор.

Эффект на GATT HR (`0x2A37`) аналогичный: нотификации продолжают приходить,
но значение внутри равно 0, потому что сенсор уже выключен.

**Повторная отправка `0x0301 [0x01]` каждую секунду тоже не решение** —
она сбрасывает замер в начало, и пульс снова показывает 0 пока не накопятся данные.

---

### Решение: раздельные poll и keepalive

Правильная архитектура — два независимых цикла в одном `while True`.

#### 1. Цикл опроса (каждые ~1–3 секунды)

Запрос истории и кэша. **Не включает** команды запуска замера.
Каждый запрос — это чтение, не управление сенсором.

```python
POLL_CMDS = [
    frame(0x0220, b""),   # GetAllRealDataFromDevice
    frame(0x0211, b""),   # GetRealBloodOxygen
    frame(0x0508, b""),   # Health_HistoryBlood
    frame(0x0506, b""),   # Health_HistoryHeart
    frame(0x0509, b""),   # Health_HistoryAll
    frame(0x051A, b""),   # Health_HistoryBloodOxygen
    frame(0x052F, b""),   # Health_HistoryComprehensiveMeasureData
]

while True:
    for cmd in POLL_CMDS:
        await client.write_gatt_char(YC_WRITE, cmd)
        await asyncio.sleep(0.1)   # минимум 80 мс между записями
    await asyncio.sleep(0.3)       # короткая пауза после полного цикла
```

Интервал между записями не менее 80 мс — BLE GATT write without response
буферизует пакеты, при слишком частой отправке часть теряется.

#### 2. Keepalive / переперезапуск (раз в 60 секунд по реальному времени)

Повторная отправка команд **запуска** замера.
Нельзя использовать счётчик итераций опроса — каждый цикл занимает
~3 секунды (7 команд × 0.1с + задержка BLE), значит `iteration % 60 == 0`
срабатывает раз в ~180 секунд, а не в 60.

```python
import time

REARM_CMDS = [
    (0x0301, b"\x01"),                                   # HeartTest вкл
    (0x0302, b"\x01"),                                   # BloodTest вкл
    (0x032E, bytes([1, 115, 80, 70, 170, 70, 30, 0])),   # StartBloodMeasurement
    (0x0309, b"\x01"),                                   # RealStream вкл
]

REARM_INTERVAL = 60.0   # секунд, реальное время
last_rearm = 0.0        # 0 — гарантирует запуск сразу при старте

while True:
    now = time.monotonic()
    if now - last_rearm >= REARM_INTERVAL:
        for dt, pl in REARM_CMDS:
            await client.write_gatt_char(YC_WRITE, frame(dt, pl))
            await asyncio.sleep(0.05)
        last_rearm = time.monotonic()   # обновлять ПОСЛЕ отправки, не до

    for cmd in POLL_CMDS:
        await client.write_gatt_char(YC_WRITE, cmd)
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
```

Ключевые детали:
- `last_rearm = 0.0` → первый keepalive происходит немедленно при старте
- `time.monotonic()` — не прыгает при смене системного времени (в отличие от `time.time()`)
- `last_rearm = time.monotonic()` ставится **после** цикла отправки — иначе время отсчитывается от начала отправки, а не от её завершения

#### Почему `asyncio.sleep`, а не `time.sleep`

`time.sleep` блокирует event loop — нотификации от часов (`on_data`) не будут
вызываться во время сна. `asyncio.sleep` отдаёт управление, позволяя
обработчикам нотификаций срабатывать параллельно.

---

### Диаграмма временной шкалы

```
t=0с    keepalive: [HEART_ON, BLOOD_ON, BP_MEASURE, REAL_STREAM]
t=0с    опрос:     [GET_REAL, GET_SPO2, HIST_BLOOD, HIST_HEART, HIST_ALL, HIST_SPO2, HIST_COMP]
t=3с    опрос:     [...]
t=6с    опрос:     [...]
...
t=60с   keepalive: [HEART_ON, BLOOD_ON, BP_MEASURE, REAL_STREAM]   ← прошло 60с реального времени
t=60с   опрос:     [...]
...
```

Без keepalive в t=60с часы к t=90с начинают слать `GATT-HR 0 BPM`.

---

## Реализация на Python

Смотри [main.py](main.py) — рабочая реализация на `bleak`:

- При `--bp` отправляет `AppStartBloodMeasurement (0x032E)`
- Каждый цикл опрашивает все команды истории
- Декодирует записи давления/пульса/всех показателей с временными метками
- Перезапускает замер каждые 60 секунд по реальному времени (`time.monotonic()`)
- Подписывается на нотификации `be940001` и `be940003` + GATT HR нотификации
