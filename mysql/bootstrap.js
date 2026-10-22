// Initialize a single-primary InnoDB Cluster from THREE fresh MySQL 8.0 nodes.
// MySQL Shell AdminAPI creates GR accounts, configures communication, and stores
// cluster metadata. Re-running this script does not re-create an existing cluster.
// Each MySQL node has a separate Docker volume and unique server-id/report-host.
const pw = os.getenv('MYSQL_ROOT_PASSWORD');
if (!pw) throw new Error('MYSQL_ROOT_PASSWORD must be set');
shell.options.useWizards = false;

function member(host) {
    return {host: host, port: 3306, user: 'root', password: pw};
}
function connectWithRetry(host) {
    for (let attempt = 0; attempt < 40; attempt++) {
        try {
            shell.connect(member(host));
            return;
        } catch (e) {
            os.sleep(2);
        }
    }
    throw new Error('Cannot connect to ' + host);
}

connectWithRetry('mysql-1');
let cluster;
try {
    cluster = dba.getCluster();
    print('Existing InnoDB Cluster found; checking membership');
} catch (e) {
    print('Creating single-primary ServLink InnoDB Cluster');
    dba.configureInstance(member('mysql-1'), {restart: false});
    cluster = dba.createCluster('servlink', {
        multiPrimary: false,
        localAddress: 'mysql-1:33061',
        exitStateAction: 'READ_ONLY'
    });
}
for (const host of ['mysql-2', 'mysql-3']) {
    const topology = cluster.status().defaultReplicaSet.topology;
    const entry = Object.entries(topology).find(([k, v]) =>
        k.startsWith(host + ':') || (v.address || '').startsWith(host + ':')
    );
    if (!entry) {
        print('Adding ' + host + ' using CLONE recovery');
        dba.configureInstance(member(host), {restart: false});
        cluster.addInstance(member(host), {
            recoveryMethod: 'clone',
            localAddress: host + ':33061',
            waitRecovery: 1
        });
    } else if (entry[1].status !== 'ONLINE') {
        print('Rejoining existing member ' + host);
        cluster.rejoinInstance(member(host));
    } else {
        print(host + ' already ONLINE');
    }
}
for (const host of ['mysql-1', 'mysql-2', 'mysql-3']) {
    connectWithRetry(host);
    const session = shell.getSession();
    session.runSql("SET PERSIST group_replication_consistency = 'BEFORE_AND_AFTER'");
    const result = session.runSql("SELECT @@group_replication_consistency AS mode").fetchOne();
    if (String(result[0]).toUpperCase() !== 'BEFORE_AND_AFTER')
        throw new Error('Consistency mode not enabled on ' + host);
}
connectWithRetry('mysql-1');
print(JSON.stringify(dba.getCluster().status(), null, 2));
print('ServLink InnoDB Cluster configured');
