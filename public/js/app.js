const DATA_URL = "../data/data.json";

const configuracoes = {
    selic_meta: {
        cor: "#08783e",
        fundo: "rgba(8, 120, 62, 0.12)",
        casasDecimais: 2,
        comparacao: "30 dias"
    },

    ipca_mensal: {
        cor: "#d97706",
        fundo: "rgba(217, 119, 6, 0.12)",
        casasDecimais: 2,
        comparacao: "dado anterior"
    },

    dolar_compra: {
        cor: "#2765b0",
        fundo: "rgba(39, 101, 176, 0.12)",
        casasDecimais: 4,
        comparacao: "30 dias"
    },

    taxa_desocupacao: {
        cor: "#7c3aed",
        fundo: "rgba(124, 58, 237, 0.12)",
        casasDecimais: 1,
        comparacao: "dado anterior"
    }
};

const estado = {
    documento: null,
    indicadorAtual: "selic_meta",
    meses: 12,
    grafico: null
};


document.addEventListener("DOMContentLoaded", iniciarAplicacao);


async function iniciarAplicacao() {
    try {
        if (typeof Chart === "undefined") {
            throw new Error(
                "Não foi possível carregar a biblioteca Chart.js."
            );
        }

        const resposta = await fetch(DATA_URL, {
            cache: "no-store"
        });

        if (!resposta.ok) {
            throw new Error(
                `Não foi possível carregar o arquivo de dados. HTTP ${resposta.status}`
            );
        }

        const documento = await resposta.json();

        if (
            !Array.isArray(documento.indicadores) ||
            documento.indicadores.length === 0
        ) {
            throw new Error(
                "O arquivo data.json não possui indicadores válidos."
            );
        }

        estado.documento = documento;

        if (
            !documento.indicadores.some(
                indicador => indicador.id === estado.indicadorAtual
            )
        ) {
            estado.indicadorAtual = documento.indicadores[0].id;
        }

        renderizarCards();
        renderizarBotoesIndicadores();
        configurarBotoesPeriodo();
        atualizarGrafico();
        atualizarStatus();

    } catch (erro) {
        mostrarErro(erro);
    }
}


function renderizarCards() {
    const container = document.getElementById("indicatorCards");

    container.innerHTML = estado.documento.indicadores
        .map(indicador => {
            const variacao = calcularVariacao(indicador);
            const ativo = indicador.id === estado.indicadorAtual;
            const rotuloCodigo =
                indicador.rotulo_codigo || "SGS";

            const codigoFonte =
                indicador.codigo_fonte || indicador.codigo_sgs;

            return `
                <button
                    type="button"
                    class="indicator-card ${ativo ? "active" : ""}"
                    data-indicator="${indicador.id}"
                >
                    <div class="card-top">
                        <span class="card-name">
                            ${indicador.nome}
                        </span>

                        <span class="card-code">
                            ${rotuloCodigo} ${codigoFonte}
                        </span>
                    </div>

                    <strong class="card-value">
                        ${formatarValor(
                            indicador.id,
                            indicador.ultimo_valor
                        )}
                    </strong>

                    <div class="card-footer">
                        <span>
                            ${formatarReferencia(indicador)}
                        </span>

                        <span class="card-change ${variacao.classe}">
                            ${variacao.texto}
                        </span>
                    </div>
                </button>
            `;
        })
        .join("");

    container
        .querySelectorAll("[data-indicator]")
        .forEach(botao => {
            botao.addEventListener("click", () => {
                selecionarIndicador(botao.dataset.indicator);
            });
        });
}


function renderizarBotoesIndicadores() {
    const container = document.getElementById("indicatorButtons");

    container.innerHTML = estado.documento.indicadores
        .map(indicador => {
            const ativo = indicador.id === estado.indicadorAtual;

            return `
                <button
                    type="button"
                    class="${ativo ? "active" : ""}"
                    data-selector="${indicador.id}"
                >
                    ${indicador.nome}
                </button>
            `;
        })
        .join("");

    container
        .querySelectorAll("[data-selector]")
        .forEach(botao => {
            botao.addEventListener("click", () => {
                selecionarIndicador(botao.dataset.selector);
            });
        });
}


function configurarBotoesPeriodo() {
    document
        .querySelectorAll("[data-months]")
        .forEach(botao => {
            botao.addEventListener("click", () => {
                document
                    .querySelectorAll("[data-months]")
                    .forEach(item => item.classList.remove("active"));

                botao.classList.add("active");

                estado.meses =
                    botao.dataset.months === "all"
                        ? null
                        : Number(botao.dataset.months);

                atualizarGrafico();
            });
        });
}


function selecionarIndicador(indicadorId) {
    estado.indicadorAtual = indicadorId;

    renderizarCards();
    renderizarBotoesIndicadores();
    atualizarGrafico();
}


function obterIndicadorAtual() {
    return estado.documento.indicadores.find(
        indicador => indicador.id === estado.indicadorAtual
    );
}


function filtrarValoresPorPeriodo(valores) {
    if (!estado.meses || valores.length === 0) {
        return valores;
    }

    const ultimaData = criarDataLocal(
        valores[valores.length - 1].data
    );

    const dataLimite = new Date(ultimaData);
    dataLimite.setMonth(
        dataLimite.getMonth() - estado.meses
    );

    return valores.filter(registro => {
        return criarDataLocal(registro.data) >= dataLimite;
    });
}


function atualizarGrafico() {
    const indicador = obterIndicadorAtual();

    if (!indicador) {
        return;
    }

    const valores = filtrarValoresPorPeriodo(
        indicador.valores
    );

    atualizarResumoGrafico(indicador);

    const labels = valores.map(registro => {
        return registro.periodo || 
        formatarDataCurta(registro.data);
    });

    const dados = valores.map(registro => registro.valor);

    const configuracao =
        configuracoes[indicador.id] || {
            cor: "#08783e",
            fundo: "rgba(8, 120, 62, 0.12)",
            casasDecimais: 2
        };

    if (estado.grafico) {
        estado.grafico.destroy();
    }

    const canvas = document.getElementById("indicatorChart");
    const contexto = canvas.getContext("2d");

    const gradiente = contexto.createLinearGradient(
        0,
        0,
        0,
        canvas.parentElement.clientHeight
    );

    gradiente.addColorStop(0, configuracao.fundo);
    gradiente.addColorStop(1, "rgba(255, 255, 255, 0)");

    estado.grafico = new Chart(contexto, {
        type: "line",

        data: {
            labels,

            datasets: [
                {
                    label: indicador.nome,
                    data: dados,
                    borderColor: configuracao.cor,
                    backgroundColor: gradiente,
                    borderWidth: 2.5,
                    fill: true,
                    tension: 0.25,
                    pointRadius: valores.length > 70 ? 0 : 3,
                    pointHoverRadius: 6,
                    pointBackgroundColor: configuracao.cor,
                    pointBorderColor: "#ffffff",
                    pointBorderWidth: 2
                }
            ]
        },

        options: {
            responsive: true,
            maintainAspectRatio: false,

            interaction: {
                intersect: false,
                mode: "index"
            },

            plugins: {
                legend: {
                    display: false
                },

                tooltip: {
                    backgroundColor: "#16251e",
                    titleColor: "#ffffff",
                    bodyColor: "#ffffff",
                    padding: 12,
                    displayColors: false,

                    callbacks: {
                        label: contextoTooltip => {
                            return formatarValor(
                                indicador.id,
                                contextoTooltip.parsed.y
                            );
                        }
                    }
                }
            },

            scales: {
                x: {
                    grid: {
                        display: false
                    },

                    border: {
                        display: false
                    },

                    ticks: {
                        color: "#87928c",
                        maxTicksLimit: 8,
                        maxRotation: 0,
                        font: {
                            size: 11
                        }
                    }
                },

                y: {
                    grace: "8%",

                    grid: {
                        color: "rgba(98, 112, 104, 0.12)"
                    },

                    border: {
                        display: false
                    },

                    ticks: {
                        color: "#87928c",

                        callback: valor => {
                            return formatarValor(
                                indicador.id,
                                valor
                            );
                        }
                    }
                }
            }
        }
    });
}


function atualizarResumoGrafico(indicador) {
    const titulo = document.getElementById("chartTitle");
    const descricao = document.getElementById(
        "chartDescription"
    );
    const valor = document.getElementById("chartValue");
    const data = document.getElementById("chartDate");
    const fonte = document.getElementById("chartSource");

    titulo.textContent = indicador.nome;

    descricao.textContent =
        indicador.descricao || "Descrição não informada.";

    valor.textContent = formatarValor(
        indicador.id,
        indicador.ultimo_valor
    );

    data.textContent = formatarReferencia(indicador);

    fonte.textContent = indicador.fonte;
}


function calcularVariacao(indicador) {
    const valores = indicador.valores;

    if (valores.length < 2) {
        return {
            texto: "Sem comparação",
            classe: "stable"
        };
    }

    const ultimo = valores[valores.length - 1];
    let referencia;

    if (indicador.id === "ipca_mensal" || indicador.id === "taxa_desocupacao") {
        referencia = valores[valores.length - 2];
    } else {
        const dataUltimo = criarDataLocal(ultimo.data);
        const dataAlvo = new Date(dataUltimo);

        dataAlvo.setDate(dataAlvo.getDate() - 30);

        referencia = valores[0];

        for (let indice = valores.length - 2; indice >= 0; indice--) {
            const dataRegistro = criarDataLocal(
                valores[indice].data
            );

            if (dataRegistro <= dataAlvo) {
                referencia = valores[indice];
                break;
            }
        }
    }

    const diferenca = ultimo.valor - referencia.valor;

    let classe = "stable";

    if (diferenca > 0) {
        classe = "up";
    } else if (diferenca < 0) {
        classe = "down";
    }

    const sinal = diferenca > 0 ? "+" : "";

    let texto;

    if (indicador.id === "dolar_compra") {
        texto =
            `${sinal}R$ ${formatarNumero(
                diferenca,
                4
            )} em 30 dias`;
    } else {
        const comparaComAnterior =
            indicador.id === "ipca_mensal" || 
            indicador.id === "taxa_desocupacao";

        const periodo = comparaComAnterior
                ? "vs. anterior"
                : "em 30 dias";

        texto =
            `${sinal}${formatarNumero(
                diferenca,
                2
            )} p.p. ${periodo}`;
    }

    return {
        texto,
        classe
    };
}


function formatarValor(indicadorId, valor) {
    const numero = Number(valor);

    if (Number.isNaN(numero)) {
        return "--";
    }

    if (indicadorId === "dolar_compra") {
        return `R$ ${formatarNumero(numero, 4)}`;
    }

    return `${formatarNumero(numero, 2)}%`;
}


function formatarNumero(valor, casasDecimais) {
    return Number(valor).toLocaleString("pt-BR", {
        minimumFractionDigits: casasDecimais,
        maximumFractionDigits: casasDecimais
    });
}


function criarDataLocal(dataIso) {
    return new Date(`${dataIso}T12:00:00`);
}

function formatarReferencia(indicador) {
    if (indicador.periodo_ultimo_valor) {
        return indicador.periodo_ultimo_valor;
    }

    return formatarData(indicador.data_ultimo_valor);
}

function formatarData(dataIso) {
    return criarDataLocal(dataIso).toLocaleDateString(
        "pt-BR",
        {
            day: "2-digit",
            month: "long",
            year: "numeric"
        }
    );
}


function formatarDataCurta(dataIso) {
    return criarDataLocal(dataIso).toLocaleDateString(
        "pt-BR",
        {
            day: "2-digit",
            month: "short"
        }
    );
}


function atualizarStatus() {
    const status = document.querySelector(".status");
    const statusText = document.getElementById("statusText");
    const lastUpdate = document.getElementById("lastUpdate");

    status.classList.add("loaded");
    statusText.textContent = "Dados carregados";

    const dataGeracao = new Date(
        estado.documento.gerado_em
    );

    lastUpdate.textContent = dataGeracao.toLocaleString(
        "pt-BR",
        {
            dateStyle: "short",
            timeStyle: "short"
        }
    );
}


function mostrarErro(erro) {
    console.error(erro);

    const mensagem = document.getElementById("errorMessage");
    const status = document.querySelector(".status");
    const statusText = document.getElementById("statusText");

    mensagem.hidden = false;
    mensagem.textContent =
        `Não foi possível carregar o painel: ${erro.message}`;

    status.classList.add("error");
    statusText.textContent = "Falha no carregamento";

    document.getElementById("indicatorCards").innerHTML = "";
}