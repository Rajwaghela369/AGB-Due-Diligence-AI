# AGB-DE profile

## Repository files

- `README.md`
- `corpus/agb-de-anonym.csv`
- `eval/bert.txt`
- `eval/gpt2.txt`
- `eval/gpt35.txt`
- `eval/roberta.txt`
- `eval/svm.txt`
- `loc_datasets/agb-de-under/dataset_dict.json`
- `loc_datasets/agb-de-under/test/data-00000-of-00001.arrow`
- `loc_datasets/agb-de-under/test/dataset_info.json`
- `loc_datasets/agb-de-under/test/state.json`
- `loc_datasets/agb-de-under/train/data-00000-of-00001.arrow`
- `loc_datasets/agb-de-under/train/dataset_info.json`
- `loc_datasets/agb-de-under/train/state.json`
- `loc_datasets/agb-de/dataset_dict.json`
- `loc_datasets/agb-de/test/data-00000-of-00001.arrow`
- `loc_datasets/agb-de/test/dataset_info.json`
- `loc_datasets/agb-de/test/state.json`
- `loc_datasets/agb-de/train/data-00000-of-00001.arrow`
- `loc_datasets/agb-de/train/dataset_info.json`
- `loc_datasets/agb-de/train/state.json`
- `responses/gpt35_responses.json`
- `scripts/anonymise/anonymise.py`
- `scripts/datasets/split_corpus.py`
- `scripts/models/bert-under.py`
- `scripts/models/bert.py`
- `scripts/models/gpt2-under.py`
- `scripts/models/gpt2.py`
- `scripts/models/gpt35-eval.py`
- `scripts/models/gpt35-retrieve.py`
- `scripts/models/roberta-under.py`
- `scripts/models/roberta.py`
- `scripts/models/svm.py`
- `scripts/stats/corpus_stats.py`
- `usage/example.py`

## GitHub corpus CSV

- source: `https://raw.githubusercontent.com/DaBr01/AGB-DE/9b847807ec38809088e496c7847f94b4266ddf56/corpus/agb-de-anonym.csv`
- rows: **3764**, columns: **8**

| column | dtype | nulls | example |
| --- | --- | --- | --- |
| `id` | int64 | 0 | 1 |
| `contract` | int64 | 0 | 1 |
| `lang` | str | 0 | de |
| `title` | str | 12 | 1. Zustandekommen des Vertrages:  |
| `text` | str | 0 | Die Darstellung der Produkte auf example.com stellt kein rechtlich bin |
| `topics` | str | 0 | conclusionOfContract |
| `subtopics` | str | 2687 | withdrawal:period |
| `void` | float64 | 0 | 0.0 |

- `void` value counts: `{0.0: 3585, 1.0: 179}`

## topics

- `payment`: 642
- `conclusionOfContract`: 557
- `withdrawal`: 506
- `delivery`: 475
- `warranty`: 314
- `liability`: 211
- `applicability`: 148
- `prices`: 147
- `retentionOfTitle`: 125
- `personalData`: 115
- `arbitration`: 97
- `applicableLaw`: 87
- `textStorage`: 58
- `placeOfJurisdiction`: 53
- `contractLanguage`: 50
- `description`: 46
- `intellectualProperty`: 39
- `disposal`: 36
- `severability`: 35
- `codeOfConduct`: 29
- `age`: 20
- `changes`: 9

## subtopics (39 distinct)

- `payment:methods`: 276
- `payment:vouchers`: 124
- `delivery:time`: 61
- `conclusionOfContract:steps`: 59
- `delivery:costs`: 52
- `withdrawal:exclusion`: 51
- `conclusionOfContract:binding`: 50
- `withdrawal:effects`: 44
- `conclusionOfContract:definition`: 39
- `prices:vat`: 36
- `withdrawal:model`: 33
- `withdrawal:period`: 31
- `payment:late`: 27
- `delivery:methods`: 26
- `withdrawal:form`: 25
- `delivery:partial`: 22
- `delivery:brokenPackaging`: 19
- `delivery:destination`: 18
- `conclusionOfContract:withdrawal`: 17
- `conclusionOfContract:restrictions`: 13
- `payment:mtehods`: 13
- `withdrawal:shippingCosts`: 11
- `warranty:period`: 10
- `withdrawal:compensation`: 7
- `withdrawal:shippingMethod`: 7
- `prices:currency`: 6
- `payment:fee`: 6
- `personalData:cookies`: 6
- `disposal:battery`: 6
- `payment:restraint`: 5
- `warranty:options`: 4
- `withdrawal:methods`: 4
- `delivery:customs`: 2
- `personalData:reason`: 1
- `personalData:information`: 1
- `personalData:usage`: 1
- `conclusionOfContract:changeOfOrder`: 1
- `delivery:price`: 1
- `withrdrawal:effects`: 1

## In-scope slice (liability or warranty)

- clauses: **523**
- void: **39**
- fragments under 40 chars: 1
- distinct contracts: 89

### subtopics within scope (5 distinct)

| subtopic | clauses | void |
| --- | --- | --- |
| `warranty:period` | 10 | 0 |
| `warranty:options` | 4 | 1 |
| `delivery:brokenPackaging` | 1 | 0 |
| `payment:vouchers` | 1 | 0 |
| `delivery:destination` | 1 | 0 |

- anonymisation placeholders still present: `{'<<NAME>>': 996, '<<STADT>>': 262, '<<STRASSE>>': 128}`

## HuggingFace splits

| split | rows | columns | void |
| --- | --- | --- | --- |
| `train` | 3004 | id, contract, lang, title, text, topics, subtopics, label | 137 |
| `test` | 755 | id, contract, lang, title, text, topics, subtopics, label | 37 |

## Joinability

- columns shared with the CSV: `['id', 'contract', 'lang', 'title', 'text', 'topics', 'subtopics']`
- split ids found in CSV: 3004 of 3004
