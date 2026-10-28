// SPDX-License-Identifier: Apache-2.0
//
// The `opseval` step of the Opseval Jenkins shared library (ADR-0058, E3).
//
//   @Library('opseval') _
//   opseval(suite: 'evals/suites/smoke.yaml', profile: 'evals/profile.yaml',
//           target: 'wss://agent.staging.example.com/ws',
//           allowedHosts: 'agent.staging.example.com')
//
// It runs the suite in the Opseval image with `docker run` (the agent needs Docker), publishes
// junit.xml with the `junit` step and archives the run folder, then fails the build unless
// Opseval exited 0:
//   1  the agent failed                  -> FAILURE
//   2  not judged                        -> FAILURE (nothing that was not judged may ship)
//   3  harness or configuration error    -> FAILURE, named as a harness error
// Never UNSTABLE: an unstable build still lets later stages and promotions run.
//
// Secrets come only from Jenkins credentials (Secret text), named by id:
// simulatorApiKeyCredentialsId, judgeApiKeyCredentialsId, agentTokenCredentialsId. They are
// bound with withCredentials and reach the container by name, never in a command line.

def call(Map config = [:]) {
    def known = [
        'suite', 'profile', 'target', 'transport', 'repeats', 'filter', 'extraArgs',
        'allowedHosts', 'simulatorBaseUrl', 'simulatorModel', 'judgeBaseUrl', 'judgeModel',
        'opsevalEnv', 'readDotenv', 'image', 'reportsDir',
        'simulatorApiKeyCredentialsId', 'judgeApiKeyCredentialsId', 'agentTokenCredentialsId',
    ]
    def unknown = config.keySet().findAll { !(it in known) }
    if (unknown) {
        error("Opseval: unknown parameter(s) ${unknown.sort().join(', ')} (exit 3, a configuration error)")
    }
    def reports = setting(config, 'reportsDir', 'opseval-reports')
    // Values reach the script through the environment, never interpolated into it.
    def vars = [
        "MODE=docker",
        "IMAGE=${setting(config, 'image', 'ghcr.io/opsedia-ai/opseval:<version>')}",
        "SUITE=${setting(config, 'suite')}",
        "PROFILE=${setting(config, 'profile')}",
        "TARGET=${setting(config, 'target')}",
        "TRANSPORT=${setting(config, 'transport', 'websocket')}",
        "REPEATS=${setting(config, 'repeats')}",
        "FILTER=${setting(config, 'filter')}",
        "EXTRA_ARGS=${setting(config, 'extraArgs')}",
        "ALLOWED_HOSTS=${setting(config, 'allowedHosts')}",
        "SIMULATOR_BASE_URL=${setting(config, 'simulatorBaseUrl')}",
        "SIMULATOR_MODEL=${setting(config, 'simulatorModel')}",
        "JUDGE_BASE_URL=${setting(config, 'judgeBaseUrl')}",
        "JUDGE_MODEL=${setting(config, 'judgeModel')}",
        "OPSEVAL_ENV=${setting(config, 'opsevalEnv')}",
        "READ_DOTENV=${setting(config, 'readDotenv', 'false')}",
        "REPORTS_DIR=${reports}",
    ]
    def secrets = []
    if (config.simulatorApiKeyCredentialsId) {
        secrets << string(credentialsId: config.simulatorApiKeyCredentialsId, variable: 'OPSEVAL_SIMULATOR_API_KEY')
    }
    if (config.judgeApiKeyCredentialsId) {
        secrets << string(credentialsId: config.judgeApiKeyCredentialsId, variable: 'OPSEVAL_JUDGE_API_KEY')
    }
    if (config.agentTokenCredentialsId) {
        secrets << string(credentialsId: config.agentTokenCredentialsId, variable: 'OPSEVAL_STATIC_TOKEN')
    }

    def script = "${pwd(tmp: true)}/opseval-ci.sh"
    writeFile(file: script, text: libraryResource('opseval/opseval-ci.sh'))
    int code = 3
    withCredentials(secrets) {
        withEnv(vars + ["OE_SCRIPT=${script}"]) {
            code = sh(returnStatus: true, script: 'bash "$OE_SCRIPT"')
        }
    }

    // Reporting never decides: a publishing failure is a warning, and the code below stands.
    try {
        // skipMarkingBuildUnstable: the build's result is Opseval's exit code, not the test count.
        junit(testResults: "${reports}/junit.xml", allowEmptyResults: true, skipMarkingBuildUnstable: true)
        def runDir = fileExists("${reports}/run-dir") ? readFile("${reports}/run-dir").trim() : ''
        def archived = ["${reports}/junit.xml", "${reports}/summary.md"]
        if (runDir) {
            archived << "${runDir}/**"
        }
        archiveArtifacts(artifacts: archived.join(','), allowEmptyArchive: true)
    } catch (InterruptedException e) {
        throw e  // an aborted build stays aborted
    } catch (Exception e) {
        echo("Opseval: warning: could not publish the reports (${e.class.simpleName}); the exit code still stands.")
    }

    switch (code) {
        case 0:
            return 0
        case 1:
            error('Opseval: the agent failed (exit 1). This blocks the release.')
            break
        case 2:
            error('Opseval: not judged (exit 2). Not an agent failure, but nothing that was not judged may ship.')
            break
        case 3:
            error('Opseval: harness or configuration error (exit 3). Not an agent failure; see the log above.')
            break
        default:
            error("Opseval did not finish (exit ${code}): a harness error.")
    }
}

// A parameter as the script's environment wants it: lists and maps one item per line.
@NonCPS
private String setting(Map config, String key, String fallback = '') {
    def v = config[key]
    if (v == null) {
        return fallback
    }
    if (v instanceof Collection) {
        return v.join('\n')
    }
    if (v instanceof Map) {
        return v.collect { k, x -> "${k}=${x}" }.join('\n')
    }
    return v.toString()
}
