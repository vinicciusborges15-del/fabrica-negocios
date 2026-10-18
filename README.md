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

O instalador verifica a versão do Ubuntu, instala dependências de sistema, baixa uma versão fixa do Codex CLI do repositório oficial OpenAI e verifica o SHA-256 do binário. Cria o usuário de serviço `fabrica`, guarda o código em `/opt/fabrica-negocios`, o executável em `/opt/fabrica-runtime/bin` e dados privados em `/var/lib/fabrica`. Realiza um teste local do sandbox, sem autenticar ou chamar modelos.

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
