# moim-scribe 작업 폴더

이 폴더는 moim-scribe 작업 폴더입니다. `inbox/`에 녹음을 넣으면 `results/`에 요약이 만들어집니다. `moim-scribe` 명령은 이 폴더에서 실행하세요.

## 사용자가 녹음의 인원수를 알려 주면

파일명에 인원수가 없는 녹음은 사용자가 인원수를 알려 줄 수 있도록 `inbox/`에서 몇 분 기다립니다. 사용자가 "방금 넣은 거 4명이야"처럼 말하면:

1. `moim-scribe waiting`을 실행합니다. 기다리는 녹음 목록이 나오고, 없으면 아무것도 나오지 않습니다.
2. `moim-scribe count 4`를 실행합니다. 기다리는 녹음이 둘 이상이거나 사용자가 녹음을 지목했으면 파일명 일부를 붙입니다: `moim-scribe count 4 "이름 일부"`
3. 결과를 사용자에게 한 줄로 알려 줍니다.

어느 녹음인지 분명하지 않으면 `count`를 실행하기 전에 물어봅니다. 기다리는 녹음이 없으면 없다고 말하고, 짐작해서 실행하지 않습니다.

## 사용자가 시키기 전에는 자료를 열지 않습니다

이 폴더에는 실제 사람들의 녹음, 전사본, 요약, 음성 지문이 있습니다. 사용자가 시키지 않았다면 아래 위치의 파일을 열거나 읽거나 검색하거나 요약하지 마세요.

- `inbox/`(안의 `_done/`, `_error/`, `_processing/` 포함), `results/`, `speakers/`, `packs/`, `models/`, `.moim-scribe-headcount/`
- `glossary.txt`, `.hf_token`, `.soniox_key`

`moim-scribe waiting`과 `moim-scribe count`는 실행해도 됩니다. 파일 이름만 보여 주고 녹음 안의 내용은 보여 주지 않습니다.
