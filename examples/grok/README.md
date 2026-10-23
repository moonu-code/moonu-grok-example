# Chamadas com IA: Moonu + Grok Voice

Conecte um assistente de voz às chamadas da [Moonu](https://moonu.com.br),
usando Grok Voice. A Moonu conecta a ligação; sua aplicação conduz o atendimento
e usa a IA contratada por você.

[Referência da API Moonu](https://dev.moonu.com.br/) ·
[Ajuda Moonu](https://doc.moonu.com.br/) ·
[Documentação Grok Voice](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech)

Este é um exemplo Python para desenvolvimento, não uma aplicação pronta de
atendimento. Pode receber e originar chamadas, consultar informações públicas da
Moonu, tentar uma transferência e encerrar a conversa. Não grava áudio localmente.

## 1. Antes de começar

Você precisa de Python 3.11 ou superior, um Ramal Moonu dedicado aos testes,
um API Token autorizado para esse ramal e uma chave da API Grok Voice.

Use uma chave da API xAI com acesso ao modelo de voz e faturamento/créditos disponíveis. Não confunda o plano do aplicativo Grok com as condições de uso da API.

Obtenha autorização das pessoas envolvidas. O áudio é enviado ao provedor de IA,
e tanto o consumo da IA quanto os serviços telefônicos podem ser cobrados.

## 2. Instalar e iniciar

Execute da raiz deste repositório:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r examples/grok/requirements.txt
.venv/bin/python -m examples.grok \
  --portal-url https://portal.moonu.com.br \
  --extension-id 2 \
  --max-call-seconds 60
```

No Windows, use `.venv\Scripts\python.exe` no lugar de `.venv/bin/python`.
O ID `2` é ilustrativo: use o **ID do ramal no Portal**, não o número SIP.
`--portal-url` recebe somente a origem, sem o sufixo `/v1`.

O terminal pede o token Moonu e a chave Grok Voice em campos ocultos. Para
automação, injete `MOONU_API_TOKEN` e `XAI_API_KEY` pelo gerenciador de segredos.
O programa não lê arquivos `.env`; não coloque chaves no código, nos argumentos
ou em capturas de tela.

Pare qualquer outro cliente desse ramal. Ligue para ele de outro telefone: o
script atende, inicia a conversa e fica disponível para a próxima ligação.
Uma ligação simultânea por ramal é o fluxo deste exemplo.

## 3. Originar uma chamada

Acrescente `--destination 500` ao comando, substituindo `500` por um ramal ou
destino autorizado na sua conta. A conversa começa somente após o atendimento.
O programa termina depois dessa chamada; não faz campanhas nem rediscagem automática.
Números externos seguem as permissões, o formato de discagem e as tarifas da conta.

## 4. Ajustar a conversa

O padrão é `grok-voice-3.0`, voz `eve`, com português brasileiro e
pronúncia “Mono” para Moonu. Use `--model NOME` e `--voice NOME` somente com
opções compatíveis com a API de voz do provedor.

O perfil de demonstração responde sobre a Moonu e pode consultar os sites
públicos oficiais. Para acrescentar contexto público, crie um arquivo UTF-8
de até 16 KiB e informe `--instructions-file caminho/contexto.md`.
Isso não permite consultar suas contas, arquivos privados ou banco de dados.
Para mudar o domínio do assistente e criar ferramentas próprias, veja
[como o exemplo está organizado](../common/README.md).

A transferência usa o código fixo `#`, seguido de `500`. Confira com o responsável
pela telefonia se esse código e destino correspondem à sua conta **antes de testar**.
O modelo não escolhe destinos arbitrários. Enviar os dígitos não confirma que uma
pessoa atendeu; não há repetição automática de uma tentativa parcial.

O desligamento por IA exige despedida/pedido claro ou confirmação de conclusão;
a aplicação aguarda o áudio pendente e verifica se houve nova fala.
Essas proteções reduzem erros, mas não substituem homologação.

A interrupção usa a detecção de fala do servidor xAI. Não há opção `--no-barge-in` neste exemplo: não presumimos que opções de outros provedores funcionem no Grok.

## 5. Acompanhar e encerrar

Os logs são JSON em inglês, com horário, provedor, etapa e identificador da chamada
quando disponíveis. Use `--log-format text` para leitura no terminal.
Não são registrados áudio, transcrições, chaves ou respostas brutas das APIs.

Dígitos recebidos ficam ocultos por padrão. `--log-dtmf` expõe somente dígitos
validados para testes; nunca use essa opção com senhas, PINs ou dados de pagamento.
DTMF recebido não é enviado automaticamente à IA.

`--max-call-seconds` aceita 1–600 segundos e inclui a espera de atendimento.
Ctrl+C encerra a sessão e tenta desligar a chamada.

**Importante: desligar a chamada ou parar o script não desconecta o ramal.**
A cobrança por tempo registrado continua até a desconexão efetiva.
Ao terminar, use a operação de registro da
[API Moonu](https://dev.moonu.com.br/) para solicitar `connected:false`
(`{"connected":false}`) e confirme o estado desconectado. O cliente não faz isso
automaticamente para não alterar a disponibilidade do ramal sem sua decisão.

## Solução de problemas

- Falha em `grok_connect` ou `grok_configure`: confira chave, acesso ao modelo,
  voz, saldo e cotas no provedor.
- Falha em `moonu_media_connect`: verifique se o endereço WSS anunciado está
  acessível de onde executa o script. Em homologação, a rede pode exigir VPN.
- HTTP 401/403: confira o token/chave e as permissões do serviço indicado na etapa.
- Áudio cortado: teste respostas curtas e registre o horário e a etapa dos logs,
  sem anexar credenciais. O limite de buffer não garante qualidade de voz.
- Sem transferência: confira o código e o destino autorizados da sua conta;
  envio de DTMF não é confirmação de atendimento.

Não há reconexão de sessão de IA nem rediscagem automática. HTTPS/WSS é exigido
fora de localhost. `--allow-insecure` é exclusivo de homologação privada e não
desabilita a validação do certificado do provedor.

Para instalação, arquitetura, testes e publicação, consulte o
[guia dos exemplos](../README.md). Mantenha `examples/common` junto deste diretório:
ele contém a conexão Moonu e as proteções compartilhadas.
