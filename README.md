# temprl — Monitor de Temperatura na Bandeja do Ubuntu 26.04

Aplicativo portátil e independente que funciona em qualquer computador com Ubuntu/GNOME, sem alterar ou remover arquivos do sistema.

## Arquivos do projeto

- `app.py`: indicador de bandeja (`AyatanaAppIndicator3` + `GTK 3`) que exibe o ícone e a temperatura atual direto na barra superior do Ubuntu, cria/atualiza automaticamente o atalho `~/.local/share/applications/temprl.desktop` conforme a pasta onde estiver e salva preferências localmente em `config.json`.
- `sensors.py`: leitura somente-leitura dos sensores em `/sys/class/hwmon` (com fallback para `/sys/class/thermal`), cálculo de máxima da sessão ou dos últimos 60 segundos e histórico recente.
- `history_window.py`: janela opcional de gráfico em tempo real (`GTK 3` + `Cairo`) no estilo do app *Resources*, mostrando a curva de temperatura, máxima e todos os sensores detectados.
- `icons/`: ícones SVG personalizados (`temprl-cool.svg`, `temprl-warm.svg`, `temprl-hot.svg`) que mudam de cor conforme a temperatura (`< 65°C` branco, `65–79°C` laranja, `≥ 80°C` vermelho).
- `temprl.sh`: script prático de inicialização.

## Como usar (neste ou em outro computador)

Basta copiar a pasta `temprl` para qualquer computador e iniciar uma primeira vez pelo terminal:

### Iniciar em segundo plano
```bash
./temprl.sh --background
```
*(Ou `python3 app.py` dentro da pasta — na primeira execução ele já ajusta permissões e cria o ícone **Temprl** na lista de Apps e em **Aplicativos iniciais de sessão** do novo computador automaticamente).*

### Iniciar no terminal (ou testar leitura de sensores)
```bash
./temprl.sh
./temprl.sh --print
```

### Encerrar
Clique no ícone de temperatura na barra superior do Ubuntu e escolha **Sair**, ou finalize via terminal:
```bash
pkill -f "temprl/app.py"
```

## Opcional: iniciar automaticamente com o Ubuntu

Na primeira execução em qualquer computador, o aplicativo cria/atualiza automaticamente o atalho `~/.local/share/applications/temprl.desktop` com o caminho exato da pasta atual. Assim, o **Temprl** aparece imediatamente na lista de **Apps** do Ubuntu e em **Aplicativos iniciais de sessão** (*Startup Applications*).
