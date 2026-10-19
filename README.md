# Fábrica de Negócios

Piloto de controlador local de experimentos com Codex CLI. Mantém uma fila de hipóteses, prepara planos e código local, acompanha custos e avalia vendas verificadas. Inclui um monitor de oportunidades públicas. Não promete renda, não publica produtos nem aceita ou entrega trabalhos automaticamente.

## Instalação nativa

Requer uma máquina Ubuntu 24.04 com Python 3.11 ou posterior, `systemd` e acesso HTTPS. O administrador deve conferir a distribuição, a memória e disco disponíveis e a conclusão da inicialização da máquina antes de instalar:

```sh
cat /etc/os-release
uname -m
free -h
df -h /
cloud-init status --wait
```

Se a imagem não incluir `cloud-init`, confira o processo de inicialização correspondente no provedor. Investigue erros antes de instalar. Este repositório não fornece acesso remoto à máquina nem configura SSH.

Revise os arquivos de `deploy/`. Depois de baixar uma revisão imutável e conferir seu SHA-256, execute no diretório extraído:

```sh
sudo sh deploy/instalar-ubuntu.sh
```

`deploy/instalar-remoto.py` é um helper de distribuição para Python 3.12 ou posterior. Baixa um pacote-fonte público por commit imutável, confere o SHA-256 esperado, extrai somente arquivos regulares e diretórios e verifica o manifesto antes de iniciar o instalador. A revisão instalada fica explicitamente fixada no helper; publicar uma nova revisão do helper não muda automaticamente o pacote-fonte instalado.

Para conferir download, hashes e lista de arquivos sem root ou alterações no sistema:

```sh
python3 deploy/instalar-remoto.py --verificar
```

Esse modo extrai em uma pasta temporária removida ao terminar; não executa `cloud-init`, `systemd-run`, serviços ou instaladores. Sem `--verificar`, o helper exige root e Ubuntu 24.04 e recusa destino existente antes de baixar o pacote. Consulta `cloud-init status --wait --format=json`, com limite de 120 segundos, antes de iniciar a unidade transitória de instalação. Aceita conclusão limpa ou conclusão com avisos recuperáveis `WARNING`/`DEPRECATED`, sempre com `status=done`, lista `errors` vazia e retorno coerente com o estado informado. Erros fatais, níveis de erro mais altos, estado incompleto, formato inválido ou timeout interrompem a preparação. O relatório mostra retorno, estado e contagens, sem imprimir textos de erros ou dados do provedor. Depois da preparação, mostra o diário da instalação. Nenhum dos modos faz login no Codex ou executa IA. O conteúdo do próprio helper também deve ser revisado e conferido antes de executá-lo.

O instalador verifica a versão do Ubuntu, instala dependências de sistema, incluindo `bubblewrap` para o sandbox Linux, baixa uma versão fixa do Codex CLI do repositório oficial OpenAI e verifica o SHA-256 do binário. Cria o usuário de serviço `fabrica`, guarda o código em `/opt/fabrica-negocios`, o executável em `/opt/fabrica-runtime/bin` e dados privados em `/var/lib/fabrica`. Realiza um teste local do sandbox, sem autenticar ou chamar modelos.

O pacote aceita somente primeira instalação: se `/opt/fabrica-negocios` já existir, o instalador encerra antes de alterar código ou dados. Não mescla arquivos antigos nem remove uma instalação anterior. Atualizações exigem revisão separada.

O serviço `fabrica-monitor` é iniciado **sem execução de IA**. Consulta um feed público periodicamente e registra o estado local. O instalador não faz login no Codex, não ativa tarefas de experimentos e não configura recebimento de pagamentos. Se o sandbox falhar, investigue o sistema e o kernel; não desative a proteção para ativar tarefas.

```sh
sudo systemctl status fabrica-monitor --no-pager
sudo journalctl -u fabrica-monitor -n 30 --no-pager
sudo systemctl stop fabrica-monitor
```

O último comando encerra o monitor. Remoção de dados ou desinstalação não é automática.

## Autenticação e primeira tarefa

O responsável deve concluir pessoalmente o login ChatGPT no servidor, conforme o fluxo oficial do Codex CLI. Não envie senhas, tokens ou códigos de login a outras pessoas. O projeto não requer uma chave da API da OpenAI nem alterna automaticamente para uma API paga.

O diagnóstico verifica apenas o estado **local** do login. O campo `auth_remotely_verified` permanece `false`; uma sessão armazenada pode estar expirada ou revogada. O sucesso de uma execução real precisa ser verificado separadamente.

Toda execução real passa por uma verificação central do login local no runner, inclusive os comandos `tick`, `run` e o worker. Somente `authenticated=true` com método `chatgpt` permite iniciar `codex exec`. Login por chave de API, ausência de login ou método desconhecido bloqueiam a execução, sem ler arquivos de credenciais nem expor tokens. `--dry-run` não consulta autenticação ou chama IA.

Antes de executar IA, o responsável deve confirmar a conclusão de `cloud-init`, o funcionamento do sandbox e o login na conta correta. Só depois dessas verificações, pode cadastrar uma hipótese e revisar a rodada prevista:

```sh
cd /opt/fabrica-negocios
sudo -H -u fabrica env PATH=/opt/fabrica-runtime/bin:/usr/bin:/bin /usr/bin/python3 -m fabrica --home /var/lib/fabrica doctor
sudo -H -u fabrica env PATH=/opt/fabrica-runtime/bin:/usr/bin:/bin /usr/bin/python3 -m fabrica --home /var/lib/fabrica new examples/experimento-demo.json
sudo -H -u fabrica env PATH=/opt/fabrica-runtime/bin:/usr/bin:/bin /usr/bin/python3 -m fabrica --home /var/lib/fabrica tick --dry-run
```

Esses comandos não iniciam uma tarefa Codex. Para uma execução manual explicitamente autorizada, use `tick` sem `--dry-run`, com `/opt/fabrica-runtime/bin` incluído no `PATH` do processo. O exemplo é uma hipótese demonstrativa, sem clientes ou demanda confirmados. Nenhuma hipótese é descoberta e cadastrada automaticamente nesta versão.

O monitor padrão não executa tarefas. A opção `--armed` do worker permite essa execução e exige uma decisão separada do responsável após as verificações. A presença dessa opção no código não significa que esteja habilitada no serviço fornecido.

## Primeira rodada limitada

O helper `deploy/primeira-rodada.py` prepara uma única rodada de planejamento para a hipótese **Orçamento Fácil para prestadores**. A proposta é uma ferramenta HTML offline para montar e imprimir orçamentos; demanda e preço ainda precisam ser testados com compradores reais.

```sh
python3 deploy/primeira-rodada.py --verificar
```

Esse modo mostra a hipótese e a execução prevista sem consultar credenciais, executar subprocessos ou alterar arquivos. A execução real exige a instalação existente, Ubuntu 24.04 e login ChatGPT do usuário `fabrica`. O helper valida o host e inicia uma unidade transitória do sistema chamada `fabrica-primeira-rodada`, executada como `fabrica`. Dentro dela, confere o sandbox, a autenticação, o estado inicial e a rodada prevista antes de cadastrar a hipótese ou executar IA. O bloqueio do controlador é mantido durante essa operação, incluindo a execução, para impedir avanço concorrente. A unidade continua se a sessão SSH cair, termina após uma rodada e não ativa o monitor para executar IA. Configuração de timeout superior a 900 segundos bloqueia esse primeiro disparo.

O cadastro e o disparo verificam o estado para evitar duplicações. Estado incompatível ou uma rodada já registrada exigem inspeção. Não remova o marcador da unidade nem tente contornar essa proteção para repetir a execução. Uma unidade iniciada não comprova conclusão: confira o estado do controlador e a presença de `PLAN.md` e `OWNER_ACTIONS.md` no workspace. Essa etapa produz o plano; construção, publicação e vendas são etapas posteriores.

```sh
sudo systemctl show fabrica-primeira-rodada.service -p ActiveState -p SubState -p Result -p ExecMainStatus
sudo journalctl -u fabrica-primeira-rodada.service -n 30 --no-pager
```

## Diagnóstico de uma rodada

`deploy/diagnosticar-rodada.py` consulta a primeira e a última execução registrada, sem repetir tarefas ou alterar seu estado. Abre o banco SQLite somente para leitura, exige ausência de journal pendente e verifica os caminhos dos logs. A saída contém metadados validados, categorias de erro, códigos conhecidos, contagens e fingerprints. Não imprime mensagens arbitrárias, prompts, respostas do agente ou credenciais.

```sh
python3 deploy/diagnosticar-rodada.py --verificar
sudo python3 deploy/diagnosticar-rodada.py
```

`--verificar` mostra somente a proposta estática, sem ler arquivos operacionais ou consultar a conta do sistema. A coleta real aceita apenas `root` ou `fabrica`; não chama modelos, serviços ou subprocessos. Categorias são pistas de diagnóstico, não comprovação isolada da causa. Se a execução estiver ativa ou o banco mudar durante a consulta, aguarde antes de coletar novamente. Preserve o experimento pausado até revisar a falha.

## Operação e limites

`python3 -m fabrica --help` lista os comandos. Use sempre `--home /var/lib/fabrica` ao operar os dados do serviço; para testes locais, escolha uma pasta separada.

Os estados principais são `queued`, `research`, `build`, `validating`, `operating`, `paused` e `rejected`. Um plano válido permite construção; a construção permite validação com compradores reais. A promoção para operação exige critérios financeiros satisfeitos e custos conferidos pelo responsável. Falhas pausam o trabalho para inspeção.

Limites técnicos iniciais ficam no arquivo `config.json` criado no diretório de dados. São valores ilustrativos configuráveis, incluindo quantidade de experimentos e execuções, orçamento-alvo, prazo e critérios de aprovação. O controlador interrompe novos trabalhos ao atingir o limite de despesas registradas. Uma despesa efetiva deve ser registrada mesmo acima do orçamento; o controle não bloqueia cartão, conta bancária ou gastos externos. O uso do Codex continua sujeito aos limites e condições da assinatura.

## Dados financeiros e permissões

Custos são lançados e conferidos manualmente. A integração financeira fica no controlador. Receita é aceita somente a partir de eventos Stripe reais, em BRL, com assinatura de webhook válida. Reembolsos e duplicidades são considerados. Não há receptor HTTP de webhooks, conta de recebimento conectada, checkout ou autorização para movimentar dinheiro neste pacote. Taxas, impostos e demais custos precisam ser registrados para que a margem tenha significado.

Os agentes recebem instruções para atuar somente no workspace e não ler credenciais, alterar contabilidade ou enviar mensagens. As ferramentas são executadas com rede desabilitada e sandbox de escrita. Isso não constitui isolamento completo de leitura do computador: o diretório pessoal e outros arquivos podem continuar acessíveis ao processo. Não execute código de terceiros sem revisão nem forneça credenciais bancárias aos agentes. Não coloque segredos nos prompts ou especificações.

O catálogo de plataformas descreve atividades que podem exigir participação humana. Pesquisas pessoais e testes de usabilidade não são respondidos em nome do dono. O feed público é uma triagem somente de leitura, sem candidatura ou entrega automática. Uma recompensa anunciada não comprova receita recebida.

## Estrutura

```text
fabrica/                  controlador e runtime Python sem dependências externas
deploy/                   instalador Ubuntu e serviço de monitoramento
examples/                 hipótese genérica de demonstração
pyproject.toml            metadados do pacote
MANIFEST.sha256           verificação dos arquivos desta distribuição
```

Não são distribuídos estado operacional, banco de dados, credenciais, históricos de execução ou dados de clientes. O instalador nativo precisa ser validado na máquina de destino; verificações locais e testes com stubs não equivalem a uma implantação ou uma operação comercial comprovada.
